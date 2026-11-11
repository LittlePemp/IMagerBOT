import asyncio
import json
import uuid

import aio_pika
from src.utils.loggers import service_logger

from src.infrastructure.services.imager_service.schemas import (
    GenerateImageRequest,
    GenerateImageResponse,
    GroupInfo,
    ListGroupsResponse,
    SyncGroupsResponse,
)


class ImagerService:
    def __init__(
        self,
        rabbitmq_url: str,
        requests_queue: str,
        rpc_timeout_seconds: float = 600,
        rpc_retries: int = 1,
    ):
        if rpc_timeout_seconds <= 0 or rpc_retries < 0:
            raise ValueError('RPC timeout must be positive and retries must be nonnegative')
        self.rabbitmq_url = rabbitmq_url
        self.requests_queue = requests_queue
        self.rpc_timeout_seconds = rpc_timeout_seconds
        self.rpc_retries = rpc_retries

    async def _rpc(self, action: str, payload: dict | None = None, request_id: str | None = None) -> dict:
        request_id = request_id or str(uuid.uuid4())
        for attempt in range(self.rpc_retries + 1):
            try:
                return await self._rpc_once(action, payload or {}, request_id)
            except (asyncio.TimeoutError, ConnectionError, aio_pika.exceptions.AMQPException) as error:
                service_logger.exception(
                    f'Imager RPC transport failed: request_id={request_id}, attempt={attempt}'
                )
                if attempt == self.rpc_retries:
                    if isinstance(error, asyncio.TimeoutError):
                        raise TimeoutError(
                            f'No response for request {request_id}; an accepted calculation may still complete. '
                            'Retry with the same request_id to retrieve its recorded result.'
                        ) from None
                    raise
                # Keep the identifier: a completed response is replayed by the
                # core instead of starting a second render after a lost reply.

    async def _rpc_once(self, action: str, payload: dict, request_id: str) -> dict:
        connection = await aio_pika.connect_robust(self.rabbitmq_url)
        try:
            channel = await connection.channel(publisher_confirms=True, on_return_raises=True)
            await channel.declare_queue(self.requests_queue, durable=True)
            callback_queue = await channel.declare_queue(exclusive=True, auto_delete=True)
            future: asyncio.Future[dict] = asyncio.get_running_loop().create_future()

            async def on_message(message: aio_pika.IncomingMessage):
                async with message.process(ignore_processed=True):
                    if message.correlation_id != request_id:
                        return
                    try:
                        body = json.loads(message.body.decode('utf-8'))
                        if not isinstance(body, dict):
                            raise ValueError('Imager RPC response must be an object')
                    except (ValueError, UnicodeError) as error:
                        service_logger.exception(f'Invalid Imager RPC response: request_id={request_id}')
                        if not future.done():
                            future.set_exception(error)
                    else:
                        if not future.done():
                            future.set_result(body)

            await callback_queue.consume(on_message)

            request_body = json.dumps(
                {
                    "action": action,
                    "payload": payload,
                    "request_id": request_id,
                }
            ).encode("utf-8")

            await channel.default_exchange.publish(
                aio_pika.Message(
                    body=request_body,
                    correlation_id=request_id,
                    reply_to=callback_queue.name,
                    content_type="application/json",
                    delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                    expiration=self.rpc_timeout_seconds * (self.rpc_retries + 1),
                ),
                routing_key=self.requests_queue,
                mandatory=True,
            )

            response = await asyncio.wait_for(future, timeout=self.rpc_timeout_seconds)
            if response.get("status") != "ok":
                service_logger.error(
                    f'Imager RPC operation failed: request_id={request_id}, error={response.get("error")}'
                )
                raise RuntimeError(response.get("error", "Unknown imager error"))
            return response
        finally:
            try:
                await connection.close()
            except Exception:
                service_logger.exception(f'Imager RPC connection cleanup failed: request_id={request_id}')

    async def list_groups(self) -> ListGroupsResponse:
        response = await self._rpc("list_groups")
        groups = [GroupInfo(**group) for group in response["data"]["groups"]]
        return ListGroupsResponse(groups=groups)

    async def generate_image(self, request: GenerateImageRequest,
                             request_id: str | None = None) -> GenerateImageResponse:
        response = await self._rpc("generate_image", request.model_dump(), request_id=request_id)
        return GenerateImageResponse(path=response["data"]["path"])

    async def sync_groups(self) -> SyncGroupsResponse:
        response = await self._rpc("sync_groups")
        return SyncGroupsResponse(message=response["data"]["message"])
