"""A dedicated container imports one catalog at a time on its own task queue."""
import asyncio
import os
import signal
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from temporalio.worker import Worker

from imager_config.loader import config
from settings import settings
from src.infrastructure.catalog_import.bootstrap import create_import_executor, create_import_repository
from src.infrastructure.temporal_client import connect_temporal_client
from src.presentation.temporal.import_activities import CatalogImportActivities
from src.presentation.temporal.import_workflows import ImportCatalogWorkflow
from src.shared_kernel.loggers import presentation_logger


async def main():
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, stop.set)
        except (NotImplementedError, RuntimeError):
            pass
    while not stop.is_set():
        try:
            client = await connect_temporal_client(settings)
            activities = CatalogImportActivities(create_import_repository(), create_import_executor())
            identity = settings.temporal_worker_identity or os.environ.get('HOSTNAME', config.temporal.worker.identity_fallback)
            with ThreadPoolExecutor(max_workers=1, thread_name_prefix='imager-catalog-import') as executor:
                worker = Worker(
                    client, task_queue=config.pack_import.task_queue, identity=identity,
                    workflows=[ImportCatalogWorkflow],
                    activities=[activities.import_catalog, activities.record_import_failure],
                    activity_executor=executor, max_concurrent_activities=1,
                    max_concurrent_workflow_tasks=config.temporal.worker.workflow_task_slots,
                    max_cached_workflows=config.temporal.worker.cached_workflows,
                    disable_eager_activity_execution=True,
                    graceful_shutdown_timeout=timedelta(seconds=config.temporal.worker.shutdown_seconds),
                )
                async with worker:
                    presentation_logger.info(f'Catalog import worker ready: queue={config.pack_import.task_queue}, slots=1')
                    await stop.wait()
                return
        except asyncio.CancelledError:
            raise
        except Exception:
            presentation_logger.exception('Catalog import worker disconnected; reconnecting')
            try:
                await asyncio.wait_for(stop.wait(), config.temporal.worker.reconnect_seconds)
            except asyncio.TimeoutError:
                pass


if __name__ == '__main__':
    asyncio.run(main())
