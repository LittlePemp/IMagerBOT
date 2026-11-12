"""A persistent container executes one synchronous render Activity at a time."""
import asyncio
import os
import signal
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from temporalio.client import Client
from temporalio.worker import Worker

from imager_config.loader import config
from settings import settings
from src.infrastructure.runtime_bootstrap import create_job_repository, create_render_executor
from src.presentation.temporal.activities import RenderActivities
from src.presentation.temporal.workflows import GenerateImageWorkflow
from src.shared_kernel.loggers import presentation_logger


async def connect_temporal():
    while True:
        try:
            return await Client.connect(
                settings.temporal_address,
                namespace=settings.temporal_namespace,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            presentation_logger.exception('Temporal is unavailable; worker reconnects')
            await asyncio.sleep(config.temporal.worker.reconnect_seconds)


async def main():
    client = await connect_temporal()
    activities = RenderActivities(
        repository=create_job_repository(), executor=create_render_executor(),
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, stop.set)
        except (NotImplementedError, RuntimeError):
            pass

    worker_config = config.temporal.worker
    identity = settings.temporal_worker_identity or os.environ.get('HOSTNAME', worker_config.identity_fallback)
    with ThreadPoolExecutor(max_workers=worker_config.activity_slots, thread_name_prefix='imager-render') as executor:
        worker = Worker(
            client,
            identity=identity,
            task_queue=settings.temporal_task_queue,
            workflows=[GenerateImageWorkflow],
            activities=[activities.render_image, activities.record_job_failure],
            activity_executor=executor,
            max_concurrent_activities=worker_config.activity_slots,
            max_concurrent_workflow_tasks=worker_config.workflow_task_slots,
            max_cached_workflows=worker_config.cached_workflows,
            disable_eager_activity_execution=worker_config.disable_eager_execution,
            graceful_shutdown_timeout=timedelta(seconds=worker_config.shutdown_seconds),
        )
        async with worker:
            presentation_logger.info(
                f'Temporal worker ready: identity={identity}, '
                f'queue={settings.temporal_task_queue}, render_slots={worker_config.activity_slots}'
            )
            await stop.wait()


if __name__ == '__main__':
    asyncio.run(main())
