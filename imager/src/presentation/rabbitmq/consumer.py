import asyncio
import json
from pathlib import Path

import aio_pika

from settings import settings
from src.application.image_builder.commands.generate_image import GenerateImageCommand
from src.application.image_builder.commands.load_missing_groups import LoadMissingGroupsCommand
from src.application.image_builder.mediator import mediator
from src.application.image_builder.queries.get_list_image_groups import GetListImageGroupsQuery
from src.presentation.api.schemas import GenerateImageRequest
from src.presentation.rabbitmq.rpc_journal import RpcJournal
from src.shared_kernel.error_handler import error_from_exception
from src.shared_kernel.loggers import presentation_logger


_in_flight: dict[str, tuple[str, asyncio.Task]] = {}


class RetryableRequestError(Exception):
    pass


def _journal() -> RpcJournal:
    return RpcJournal(
        Path(settings.file_path_prefix) / settings.generated_images_path,
        ttl_seconds=getattr(settings, 'rpc_result_ttl_seconds', 86400),
        max_entries=getattr(settings, 'rpc_result_max_entries', 1000),
    )


def _error_response(error) -> dict:
    return {
        'status': 'error',
        'error': str(error),
        'retryable': bool(getattr(error, 'retryable', False)),
        'code': getattr(error, 'code', 'generation_failed'),
    }


async def _handle_request(action: str, payload: dict) -> dict:
    if action == "generate_image":
        request = GenerateImageRequest(**payload)
        command = GenerateImageCommand(
            image_path=request.image_path,
            group_name=request.group_name,
            insertion_format=request.insertion_format,
            alpha_channel=request.alpha_channel,
            noise_level=request.noise_level,
            cell_size=request.cell_size,
            result_size=request.result_size,
            output_name=request.output_name,
        )
        result = await asyncio.to_thread(mediator.send, command)
        if not result.is_success:
            presentation_logger.error(f'RabbitMQ generation failed: {result.error}')
            return _error_response(result.error)
        return {"status": "ok", "data": {"path": result.value}}

    if action == "list_groups":
        groups = await asyncio.to_thread(mediator.send, GetListImageGroupsQuery())
        return {"status": "ok", "data": {"groups": groups}}

    if action == "sync_groups":
        result = await asyncio.to_thread(mediator.send, LoadMissingGroupsCommand())
        if not result.is_success:
            presentation_logger.error(f'RabbitMQ synchronization failed: {result.error}')
            return _error_response(result.error)
        return {"status": "ok", "data": {"message": result.value}}

    return {"status": "error", "error": f"Unsupported action: {action}"}


async def _execute_generation(request_id: str, fingerprint: str, payload: dict, journal: RpcJournal) -> dict:
    cached = await asyncio.to_thread(journal.get, request_id, fingerprint)
    if cached is not None:
        presentation_logger.info(f'Replaying completed RPC response: request_id={request_id}')
        return cached
    response = await _handle_request('generate_image', payload)
    if response.get('retryable'):
        raise RetryableRequestError(response['error'])
    # Persist before replying/acknowledging, so a lost reply can be requested again.
    # A process crash between rendering and this write may still repeat the render.
    await asyncio.to_thread(journal.store, request_id, fingerprint, response)
    return response


async def _generation_response(request_id: str, payload: dict, journal: RpcJournal) -> dict:
    fingerprint = journal.fingerprint('generate_image', payload)
    current = _in_flight.get(request_id)
    if current is not None:
        if current[0] != fingerprint:
            raise ValueError('request_id is already running with different parameters')
        return await asyncio.shield(current[1])
    task = asyncio.create_task(_execute_generation(request_id, fingerprint, payload, journal))
    _in_flight[request_id] = (fingerprint, task)

    def finished(completed):
        if _in_flight.get(request_id, (None, None))[1] is completed:
            _in_flight.pop(request_id, None)
        if not completed.cancelled():
            completed.exception()  # Retrieve exceptions even if the client disappeared.

    task.add_done_callback(finished)
    return await asyncio.shield(task)


async def _retry_message(message, exchange, attempt: int) -> None:
    await asyncio.sleep(min(0.25 * (2 ** attempt), 2))
    headers = dict(message.headers or {})
    headers['x-imager-retry'] = attempt + 1
    await exchange.publish(
        aio_pika.Message(
            body=message.body,
            correlation_id=message.correlation_id,
            reply_to=message.reply_to,
            content_type='application/json',
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            headers=headers,
            expiration=message.expiration,
        ),
        routing_key=settings.rabbitmq_requests_queue,
        mandatory=True,
    )
    await message.ack()


def _retry_count(message) -> int:
    try:
        return max(0, int((message.headers or {}).get('x-imager-retry', 0)))
    except (TypeError, ValueError, OverflowError):
        return getattr(settings, 'rabbitmq_max_retries', 2)


async def _process_message(message: aio_pika.IncomingMessage, exchange: aio_pika.abc.AbstractExchange):
    request_id = message.correlation_id or ''
    action = None
    completed_generation = False
    try:
        if len(message.body) > 65536:
            raise ValueError('RPC request exceeds 64 KiB')
        request = json.loads(message.body.decode('utf-8'))
        if not isinstance(request, dict):
            raise ValueError('RPC request must be an object')
        request_id = request.get('request_id') or request_id
        if not isinstance(request_id, str) or not request_id or len(request_id) > 128:
            raise ValueError('RPC request_id is required and must contain at most 128 characters')
        action = request.get('action')
        payload = request.get('payload', {})
        if not isinstance(payload, dict):
            raise ValueError('RPC payload must be an object')
        if action == 'generate_image':
            response = await _generation_response(request_id, payload, _journal())
            completed_generation = True
        else:
            response = await _handle_request(action, payload)
        if response.get('retryable'):
            raise RetryableRequestError(response['error'])
    except asyncio.CancelledError:
        # Leaving the delivery unacknowledged lets the broker redeliver it.
        raise
    except Exception as error:
        retryable = isinstance(error, RetryableRequestError)
        retryable = retryable or error_from_exception(error).retryable
        retryable = retryable or bool(getattr(error, 'retryable', False))
        attempt = _retry_count(message)
        presentation_logger.exception(
            f'RPC failed: request_id={request_id}, attempt={attempt}, retryable={retryable}'
        )
        if retryable and attempt < getattr(settings, 'rabbitmq_max_retries', 2):
            try:
                await _retry_message(message, exchange, attempt)
            except Exception:
                presentation_logger.exception(f'RPC retry could not be published: request_id={request_id}')
                await message.nack(requeue=True)
            return
        response = {
            'status': 'error',
            'error': str(error),
            'retryable': False,
            'code': 'retry_exhausted' if retryable else 'invalid_request',
        }

    try:
        if message.reply_to:
            await exchange.publish(
                aio_pika.Message(
                    body=json.dumps(response).encode('utf-8'),
                    correlation_id=message.correlation_id,
                    content_type='application/json',
                    delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                ),
                routing_key=message.reply_to,
                # The old callback queue may have expired. Completed generation
                # responses remain in the journal and are replayed on a retry.
                mandatory=not completed_generation,
            )
    except Exception:
        presentation_logger.exception(f'RPC response delivery failed: request_id={request_id}')
        if not completed_generation:
            attempt = _retry_count(message)
            if attempt < getattr(settings, 'rabbitmq_max_retries', 2):
                try:
                    await _retry_message(message, exchange, attempt)
                except Exception:
                    presentation_logger.exception(f'RPC delivery retry could not be published: request_id={request_id}')
                    await message.nack(requeue=True)
            else:
                presentation_logger.error(f'RPC reply delivery retries exhausted: request_id={request_id}')
                await message.reject(requeue=False)
            return
    await message.ack()


async def run_consumer():
    connection = await aio_pika.connect_robust(settings.rabbitmq_url)
    channel = await connection.channel(publisher_confirms=True, on_return_raises=True)
    await channel.set_qos(prefetch_count=settings.rabbitmq_prefetch_count)
    queue = await channel.declare_queue(settings.rabbitmq_requests_queue, durable=True)
    await queue.consume(lambda message: _process_message(message, channel.default_exchange))

    presentation_logger.info(
        f"RabbitMQ consumer started on queue {settings.rabbitmq_requests_queue}"
    )

    try:
        await asyncio.Future()
    finally:
        await connection.close()
