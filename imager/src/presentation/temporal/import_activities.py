"""Synchronous import activities executed outside the worker's event loop."""
import time

from temporalio import activity
from temporalio.exceptions import ApplicationError

from imager_config.loader import config
from imager_contracts.catalog_import import CatalogImportRequest, import_fingerprint
from src.application.catalog_import.errors import CatalogImportBusy, LostCatalogImport
from src.infrastructure.jobs.mongo_jobs import LostExecution
from src.presentation.temporal.activities import LeaseHeartbeat
from src.shared_kernel.loggers import presentation_logger


class CatalogImportActivities:
    def __init__(self, repository, executor):
        self.repository, self.executor = repository, executor

    @activity.defn(name='import_catalog')
    def import_catalog(self, request: dict, import_policy: dict) -> dict:
        token = None
        import_id = request.get('import_id', 'invalid') if isinstance(request, dict) else 'invalid'
        snapshot = None
        try:
            snapshot = CatalogImportRequest.from_dict(request).to_dict()
            interval = min(config.jobs.heartbeat_interval_seconds,
                           import_policy['heartbeat_seconds'] / config.jobs.heartbeat_renew_divisor)
            while True:
                try:
                    claim = self.repository.claim_execution(snapshot)
                    break
                except CatalogImportBusy:
                    if activity.is_cancelled():
                        raise LostCatalogImport('Temporal отменил ожидающий импорт.')
                    activity.heartbeat({'import_id': import_id, 'phase': 'waiting_for_execution_lease'})
                    time.sleep(min(config.jobs.claim_wait_seconds, interval))
            if claim['already_completed']:
                return claim['result']
            token = claim['token']
            with LeaseHeartbeat(self.repository, import_id, token, interval=interval) as heartbeat:
                result = self.executor.execute(snapshot, token, heartbeat.progress)
                heartbeat.check()
            return self.repository.complete(import_id, token, result)
        except Exception as error:
            presentation_logger.exception(f'Catalog import Activity failed: import_id={import_id}')
            retryable = bool(getattr(error, 'retryable', False))
            if not isinstance(error, (ValueError, MemoryError, ApplicationError)) and not hasattr(error, 'retryable'):
                retryable = True
            if isinstance(error, LostExecution):
                retryable = True
            if token is not None:
                if retryable and activity.info().attempt < import_policy['maximum_attempts']:
                    self.repository.retry_execution(import_id, token, error)
                else:
                    self.repository.fail(import_id, token, error)
            elif snapshot is not None and not retryable:
                existing = self.repository.get(import_id)
                if existing and existing.get('payload_hash') == import_fingerprint(snapshot):
                    self.repository.finalize_failure(import_id, error)
            raise ApplicationError(
                str(error), type='TransientCatalogImportError' if retryable else 'CatalogImportRejected',
                non_retryable=not retryable,
            ) from error

    @activity.defn(name='record_import_failure')
    def record_import_failure(self, request: dict) -> dict:
        return self.repository.finalize_failure(request['import_id'], request.get('error', 'Import failed'))
