"""Temporal adapter: sync rendering with a renewable external execution claim."""
import contextvars
import threading
import time

from temporalio import activity
from temporalio.exceptions import ApplicationError

from imager_contracts import JobRequest
from imager_contracts.workflow_versions import LEGACY_RENDER_POLICY_V1
from imager_config.loader import config
from src.infrastructure.jobs.mongo_jobs import JobBusy, LostExecution
from src.infrastructure.jobs.storage_hygiene import has_job_reference
from src.shared_kernel.loggers import presentation_logger


class LeaseHeartbeat:
    def __init__(self, repository, job_id, token, interval=None):
        self.repository, self.job_id, self.token = repository, job_id, token
        self.interval = config.jobs.heartbeat_interval_seconds if interval is None else interval
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.details = {'job_id': job_id, 'stage': 'starting'}
        self.failure = None

    def pulse(self):
        if activity.is_cancelled():
            raise LostExecution('Temporal cancelled the previous attempt')
        with self.lock:
            details = dict(self.details)
        self.repository.heartbeat_execution(self.job_id, self.token, details)
        activity.heartbeat(details)

    def progress(self, details):
        self.check()
        with self.lock:
            self.details = dict(details)

    def check(self):
        if self.failure is not None:
            raise LostExecution(f'Cannot maintain execution ownership: {self.failure}')
        if activity.is_cancelled():
            raise LostExecution('Temporal requested cancellation of this attempt')

    def __enter__(self):
        self.pulse()
        context = contextvars.copy_context()

        def run():
            while not self.stop.wait(self.interval):
                try:
                    self.pulse()
                except Exception as error:
                    presentation_logger.exception(f'Execution heartbeat failed: job_id={self.job_id}')
                    self.failure = error
                    return

        self.thread = threading.Thread(
            target=lambda: context.run(run), name='imager-lease-heartbeat', daemon=True,
        )
        self.thread.start()
        return self

    def __exit__(self, *exception):
        self.stop.set()
        self.thread.join(timeout=config.jobs.heartbeat_join_seconds)


class RenderActivities:
    def __init__(self, repository, executor):
        self.repository, self.executor = repository, executor

    @activity.defn(name='render_image')
    def render_image(self, request: dict, render_policy: dict | None = None) -> dict:
        render_policy = LEGACY_RENDER_POLICY_V1 if render_policy is None else render_policy
        token = None
        job_id = request.get('job_id', 'invalid') if isinstance(request, dict) else 'invalid'
        try:
            heartbeat_interval = min(
                config.jobs.heartbeat_interval_seconds,
                render_policy['heartbeat_seconds'] / config.jobs.heartbeat_renew_divisor,
            )
            snapshot = JobRequest.from_dict(request).to_dict()
            while True:
                try:
                    claim = self.repository.claim_execution(snapshot)
                    break
                except JobBusy:
                    # A timed-out attempt may still own its external lease.
                    # Wait with heartbeats, rather than burning all retries in
                    # a few seconds before that lease can expire.
                    if activity.is_cancelled():
                        raise LostExecution('Cancelled while waiting for an execution lease')
                    activity.heartbeat({'job_id': job_id, 'stage': 'waiting_for_execution_lease'})
                    time.sleep(min(config.jobs.claim_wait_seconds, heartbeat_interval))
            if claim['already_completed']:
                return claim['result']
            token = claim['token']
            uploaded = None
            try:
                with LeaseHeartbeat(self.repository, job_id, token, interval=heartbeat_interval) as heartbeat:
                    uploaded = self.executor.execute(snapshot, token, heartbeat.progress)
                    heartbeat.check()
                reference = self.repository.complete(job_id, token, uploaded)
            except Exception:
                if uploaded is not None:
                    self._discard_unpublished_result(job_id, token, uploaded)
                raise
            presentation_logger.info(f'Render completed: job_id={job_id}, fence={claim["fence"]}')
            return reference
        except Exception as error:
            presentation_logger.exception(f'Render Activity failed: job_id={job_id}')
            retryable = bool(getattr(error, 'retryable', False))
            # Requests and renderer policy failures must not be retried. Unknown
            # infrastructure failures get bounded retries, never silent success.
            if not isinstance(error, (ValueError, ApplicationError)) and not hasattr(error, 'retryable'):
                retryable = True
            attempt = activity.info().attempt
            if token is not None:
                if retryable and attempt < render_policy['maximum_attempts']:
                    self.repository.retry_execution(job_id, token, error)
                else:
                    self.repository.fail(job_id, token, error)
            raise ApplicationError(
                str(error), type='TransientRenderError' if retryable else 'RenderRejected',
                non_retryable=not retryable,
            ) from error

    def _discard_unpublished_result(self, job_id, execution_token, reference):
        """Delete only an attempt that has definitely lost publication rights.

        A timed-out Mongo completion can still commit after its client failed.
        A read of result=None alone does not prove abandonment. Keep an object
        while this token may still publish it, and let the grace-period janitor
        resolve uncertain outcomes after the job reaches a terminal state.
        """
        try:
            expected_key = config.storage.result_key_template.format(
                job_id=job_id, execution_token=execution_token,
                result_filename=config.storage.result_filename,
            )
            if reference.get('key') != expected_key:
                return
            current = self.repository.collection.find_one(
                {'_id': job_id}, {'status': 1, 'execution.token': 1, 'result': 1},
            )
            if current is None or (current.get('result') or {}).get('key') == reference.get('key'):
                return
            owner = (current.get('execution') or {}).get('token')
            terminal = current.get('status') in ('completed', 'failed')
            replaced = current.get('status') == 'running' and owner and owner != execution_token
            if not terminal and not replaced:
                return
            if has_job_reference(self.repository.collection, reference):
                return
            self.executor.store.delete_file(reference)
            presentation_logger.info(f'Removed unpublished result of a lost attempt: job_id={job_id}')
        except Exception:
            presentation_logger.exception(f'Cannot remove unpublished result: job_id={job_id}')

    @activity.defn(name='record_job_failure')
    def record_job_failure(self, request: dict) -> dict:
        return self.repository.finalize_failure(request['job_id'], request.get('error', 'Render failed'))
