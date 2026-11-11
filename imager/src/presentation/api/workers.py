import asyncio

from fastapi import HTTPException
from settings import settings
from src.application.image_builder.commands.generate_image import GenerateImageCommand
from src.application.image_builder.mediator import mediator
from src.presentation.api.schemas import GenerateImageResponse
from src.shared_kernel.loggers import presentation_logger


# The generation handler owns the process-wide generation gate. A single HTTP
# worker keeps waiting jobs from occupying executor threads behind that gate.
queue = asyncio.Queue(maxsize=getattr(settings, 'api_queue_maxsize', 8))
_worker_tasks: list[asyncio.Task] = []
_accepting_requests = False


def accepting_requests() -> bool:
    return _accepting_requests and any(not task.done() for task in _worker_tasks)


def generation_queue() -> asyncio.Queue:
    return queue


def _deliver(future: asyncio.Future, *, value=None, error=None) -> None:
    """A disconnected/timed-out client must never stop a worker."""
    if future.done():
        return
    if error is not None:
        future.set_exception(error)
    else:
        future.set_result(value)


async def worker(request_queue: asyncio.Queue | None = None):
    request_queue = request_queue if request_queue is not None else queue
    while True:
        request = await request_queue.get()
        future = request['future']
        request_id = request.get('request_id', 'unknown')
        try:
            if future.cancelled():
                continue
            command = GenerateImageCommand(
                image_path=request['image_path'],
                group_name=request['group_name'],
                insertion_format=request['insertion_format'],
                alpha_channel=request['alpha_channel'],
                noise_level=request['noise_level'],
                cell_size=request['cell_size'],
                result_size=request['result_size'],
                output_name=request.get('output_name'),
            )
            result = await asyncio.to_thread(mediator.send, command)
            if result.is_success:
                _deliver(future, value=GenerateImageResponse(path=result.value))
                presentation_logger.info(f'HTTP generation completed: request_id={request_id}')
            else:
                presentation_logger.error(
                    f'HTTP generation failed: request_id={request_id}, error={result.error}'
                )
                status = 503 if getattr(result.error, 'retryable', False) else 400
                _deliver(future, error=HTTPException(status_code=status, detail=str(result.error)))
        except asyncio.CancelledError:
            presentation_logger.warning(
                f'HTTP worker stopping: request_id={request_id}; an active executor calculation may finish'
            )
            _deliver(future, error=HTTPException(status_code=503, detail='Generator is shutting down'))
            raise
        except Exception:
            presentation_logger.exception(f'HTTP generation failed: request_id={request_id}')
            _deliver(future, error=HTTPException(status_code=500, detail='Image generation failed'))
        finally:
            request_queue.task_done()


def start_workers():
    global _accepting_requests, queue
    if any(not task.done() for task in _worker_tasks):
        return
    _worker_tasks.clear()
    queue = asyncio.Queue(maxsize=getattr(settings, 'api_queue_maxsize', 8))
    _accepting_requests = True
    _start_worker()


def _start_worker():
    task = asyncio.create_task(worker(), name='imager-http-worker')
    _worker_tasks.append(task)

    def finished(completed):
        if not completed.cancelled():
            error = completed.exception()
            if error is not None:
                presentation_logger.error(f'HTTP worker exited unexpectedly: {error!r}')
        if _accepting_requests:
            if completed in _worker_tasks:
                _worker_tasks.remove(completed)
            _start_worker()

    task.add_done_callback(finished)


async def stop_workers():
    global _accepting_requests
    _accepting_requests = False
    timeout = getattr(settings, 'api_shutdown_timeout_seconds', 30)
    try:
        await asyncio.wait_for(queue.join(), timeout=timeout)
    except asyncio.TimeoutError:
        presentation_logger.warning('HTTP shutdown deadline reached; queued requests are rejected')
        while True:
            try:
                request = queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            _deliver(request['future'], error=HTTPException(status_code=503, detail='Generator is shutting down'))
            queue.task_done()
    finally:
        for task in _worker_tasks:
            task.cancel()
        await asyncio.gather(*_worker_tasks, return_exceptions=True)
        _worker_tasks.clear()
