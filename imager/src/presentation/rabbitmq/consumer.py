import asyncio
import json

import aio_pika

from settings import settings
from src.application.image_builder.commands.generate_image import GenerateImageCommand
from src.application.image_builder.commands.load_missing_groups import LoadMissingGroupsCommand
from src.application.image_builder.mediator import mediator
from src.application.image_builder.queries.get_list_image_groups import GetListImageGroupsQuery
from src.presentation.api.schemas import GenerateImageRequest
from src.shared_kernel.loggers import presentation_logger


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
            return {"status": "error", "error": str(result.error)}
        return {"status": "ok", "data": {"path": result.value}}

    if action == "list_groups":
        groups = await asyncio.to_thread(mediator.send, GetListImageGroupsQuery())
        return {"status": "ok", "data": {"groups": groups}}

    if action == "sync_groups":
        result = await asyncio.to_thread(mediator.send, LoadMissingGroupsCommand())
        if not result.is_success:
            return {"status": "error", "error": str(result.error)}
        return {"status": "ok", "data": {"message": result.value}}

    return {"status": "error", "error": f"Unsupported action: {action}"}


async def _process_message(message: aio_pika.IncomingMessage, exchange: aio_pika.abc.AbstractExchange):
    async with message.process(ignore_processed=True):
        try:
            request = json.loads(message.body.decode("utf-8"))
            action = request.get("action")
            payload = request.get("payload", {})
            response = await _handle_request(action, payload)
        except Exception as e:
            presentation_logger.error(f"RabbitMQ consumer failed: {e}")
            response = {"status": "error", "error": str(e)}

        if message.reply_to:
            await exchange.publish(
                aio_pika.Message(
                    body=json.dumps(response).encode("utf-8"),
                    correlation_id=message.correlation_id,
                    content_type="application/json",
                ),
                routing_key=message.reply_to,
            )


async def run_consumer():
    connection = await aio_pika.connect_robust(settings.rabbitmq_url)
    channel = await connection.channel()
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
