"""Deliver Mongo start-outbox entries to Temporal, without rendering images."""
import asyncio
import signal
import time

from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError

from imager_config.loader import config
from settings import settings
from src.infrastructure.runtime_bootstrap import create_job_repository, create_object_store
from src.infrastructure.jobs.image_retention import ImageRetention
from src.infrastructure.jobs.storage_hygiene import StorageHygiene
from src.infrastructure.catalog_import.bootstrap import create_import_repository
from src.infrastructure.temporal_client import connect_temporal_client
from src.shared_kernel.loggers import presentation_logger


async def dispatch_once(repository, client) -> bool:
    event = await asyncio.to_thread(repository.claim_dispatch)
    if event is None:
        return False
    job_id = event['job_id']
    token = event['dispatch_token']
    workflow_id = event['workflow_id']
    request = event['request']
    try:
        try:
            await asyncio.to_thread(repository.validate_dispatch, event)
        except ValueError as error:
            presentation_logger.error(f'Generation inbox rejected: job_id={job_id}, error={error}')
            await asyncio.to_thread(repository.reject_dispatch, job_id, token, str(error))
            return True
        try:
            await client.start_workflow(
                'GenerateImageWorkflow',
                args=[request, event['execution_policy']],
                id=workflow_id,
                task_queue=settings.temporal_task_queue,
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                memo={
                    'job_id': job_id,
                    'source': request['source'],
                    'parameters': request['parameters'],
                    'catalog_manifest': request['catalog_manifest'],
                    'execution_policy': event['execution_policy'],
                },
                static_summary=f'IMager: {job_id}',
                static_details=(
                    'Input contains the original MinIO reference and an immutable '
                    'parameter/catalog snapshot. Activity execution is limited '
                    'to one image per worker container.'
                ),
            )
        except WorkflowAlreadyStartedError:
            # A prior dispatcher may have crashed after Temporal accepted the
            # start but before the Mongo outbox acknowledgement.
            presentation_logger.info(f'Workflow already accepted: job_id={job_id}')
        acknowledged = await asyncio.to_thread(
            repository.mark_dispatched, job_id, token, workflow_id,
        )
        if acknowledged:
            presentation_logger.info(f'Start outbox acknowledged: job_id={job_id}')
        else:
            presentation_logger.warning(
                f'Workflow accepted but dispatch lease changed: job_id={job_id}; '
                'the current owner will acknowledge the same workflow'
            )
    except asyncio.CancelledError:
        raise
    except Exception as error:
        presentation_logger.exception(f'Cannot dispatch generation: job_id={job_id}')
        await asyncio.to_thread(repository.defer_dispatch, job_id, token, str(error))
    return True


async def reconcile_once(repository, client):
    """Recover terminal jobs whose worker could not publish its final state.

    A UI termination bypasses workflow/Activity cleanup. Describe only workflows
    already accepted by Temporal; connection errors never imply job failure.
    """
    jobs = await asyncio.to_thread(
        repository.unfinished_jobs, limit=config.temporal.dispatcher.reconcile_batch_size,
    )
    for job in jobs:
        job_id = job['job_id']
        workflow_id = job['workflow_id']
        try:
            description = await client.get_workflow_handle(workflow_id).describe()
            status = description.status.name if description.status is not None else ''
            if status in {'FAILED', 'CANCELED', 'TERMINATED', 'TIMED_OUT'}:
                await asyncio.to_thread(repository.finalize_failure, job_id, status)
                presentation_logger.warning(
                    f'Reconciled terminal workflow: job_id={job_id}, status={status}'
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            presentation_logger.exception(f'Cannot reconcile workflow: job_id={job_id}')


async def dispatch_import_once(repository, client):
    event = await asyncio.to_thread(repository.claim_dispatch)
    if event is None:
        return False
    import_id, token = event['import_id'], event['dispatch_token']
    try:
        try:
            await asyncio.to_thread(repository.validate_dispatch, event)
            if not isinstance(event['workflow_id'], str) or not event['workflow_id']:
                raise ValueError('Import workflow identity is missing')
        except ValueError as error:
            await asyncio.to_thread(repository.reject_dispatch, import_id, token, str(error))
            presentation_logger.error(f'Import inbox rejected: import_id={import_id}')
            return True
        request = event['request']
        try:
            await client.start_workflow(
                'ImportCatalogWorkflow', args=[request, event['execution_policy']],
                id=event['workflow_id'], task_queue=config.pack_import.task_queue,
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                memo={'import_id': import_id, 'source': request['source'],
                      'group_name': request['group_name'], 'display_name': request['display_name'],
                      'actor_id': request['actor_id'], 'execution_policy': event['execution_policy']},
                static_summary=f'Import: {request["display_name"]}',
                static_details='Validate the ZIP and publish a new immutable catalog from MinIO in the dedicated import worker.',
            )
        except WorkflowAlreadyStartedError:
            presentation_logger.info(f'Import workflow already accepted: import_id={import_id}')
        await asyncio.to_thread(repository.mark_dispatched, import_id, token, event['workflow_id'])
    except asyncio.CancelledError:
        raise
    except Exception as error:
        presentation_logger.exception(f'Cannot dispatch import: import_id={import_id}')
        await asyncio.to_thread(repository.defer_dispatch, import_id, token, str(error))
    return True


async def reconcile_import_once(repository, client):
    jobs = await asyncio.to_thread(repository.unfinished_jobs, limit=config.temporal.dispatcher.reconcile_batch_size)
    for job in jobs:
        try:
            description = await client.get_workflow_handle(job['workflow_id']).describe()
            status = description.status.name if description.status is not None else ''
            if status in {'FAILED', 'CANCELED', 'TERMINATED', 'TIMED_OUT'}:
                await asyncio.to_thread(repository.finalize_failure, job['import_id'], status)
        except asyncio.CancelledError:
            raise
        except Exception:
            presentation_logger.exception(f'Cannot reconcile import: import_id={job["import_id"]}')


async def run_dispatchers(repository, import_repository, stop):
    while not stop.is_set():
        try:
            client = await connect_temporal_client(settings)
            last_reconcile = 0
            while not stop.is_set():
                if time.monotonic() - last_reconcile >= config.temporal.dispatcher.reconcile_seconds:
                    await reconcile_once(repository, client)
                    await reconcile_import_once(import_repository, client)
                    last_reconcile = time.monotonic()
                delivered = await dispatch_once(repository, client)
                imported = await dispatch_import_once(import_repository, client)
                if not delivered and not imported:
                    try:
                        await asyncio.wait_for(
                            stop.wait(), timeout=settings.temporal_dispatch_poll_seconds,
                        )
                    except asyncio.TimeoutError:
                        pass
        except asyncio.CancelledError:
            raise
        except Exception:
            presentation_logger.exception('Dispatcher connection/claim failed; retrying')
            try:
                await asyncio.wait_for(stop.wait(), timeout=config.temporal.dispatcher.reconnect_seconds)
            except asyncio.TimeoutError:
                pass


async def retain_images(repository, stop):
    if not config.retention.enabled:
        return
    while not stop.is_set():
        try:
            store = await asyncio.to_thread(create_object_store)
            cleaner = ImageRetention(repository.collection, store)
            await asyncio.to_thread(cleaner.initialize_indexes)
            await cleaner.run(stop)
        except asyncio.CancelledError:
            raise
        except Exception:
            presentation_logger.exception('Cannot initialize generation artifact retention')
            try:
                await asyncio.wait_for(stop.wait(), timeout=config.retention.poll_seconds)
            except asyncio.TimeoutError:
                pass


async def keep_storage_clean(repository, import_repository, stop):
    """Import archives, unreferenced results and expired job documents."""
    if not config.retention.enabled:
        return
    while not stop.is_set():
        try:
            store = await asyncio.to_thread(create_object_store)
            await StorageHygiene(repository.collection, import_repository.collection, store).run(stop)
        except asyncio.CancelledError:
            raise
        except Exception:
            presentation_logger.exception('Cannot initialize storage hygiene')
            try:
                await asyncio.wait_for(stop.wait(), timeout=config.retention.poll_seconds)
            except asyncio.TimeoutError:
                pass


async def main():
    repository = create_job_repository()
    import_repository = create_import_repository()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, stop.set)
        except (NotImplementedError, RuntimeError):
            pass
    async with asyncio.TaskGroup() as tasks:
        tasks.create_task(retain_images(repository, stop), name='generation-artifact-retention')
        tasks.create_task(keep_storage_clean(repository, import_repository, stop), name='storage-hygiene')
        await run_dispatchers(repository, import_repository, stop)


if __name__ == '__main__':
    asyncio.run(main())
