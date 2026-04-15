import asyncio
import json
import uuid

import aio_pika

from src.infrastructure.services.imager_service.schemas import (
    GenerateImageRequest,
    GenerateImageResponse,
    GroupInfo,
    ListGroupsResponse,
    SyncGroupsResponse,
)


class ImagerService:
    def __init__(self, rabbitmq_url: str, requests_queue: str):
        self.rabbitmq_url = rabbitmq_url
        self.requests_queue = requests_queue

    async def _rpc(self, action: str, payload: dict | None = None) -> dict:
        connection = await aio_pika.connect_robust(self.rabbitmq_url)
        try:
            channel = await connection.channel()
            callback_queue = await channel.declare_queue(exclusive=True, auto_delete=True)
            correlation_id = str(uuid.uuid4())
            future: asyncio.Future[dict] = asyncio.get_running_loop().create_future()

            async def on_message(message: aio_pika.IncomingMessage):
                async with message.process(ignore_processed=True):
                    if message.correlation_id != correlation_id:
                        return
                    body = json.loads(message.body.decode("utf-8"))
                    if not future.done():
                        future.set_result(body)

            await callback_queue.consume(on_message)

            request_body = json.dumps(
                {
                    "action": action,
                    "payload": payload or {},
                }
            ).encode("utf-8")

            await channel.default_exchange.publish(
                aio_pika.Message(
                    body=request_body,
                    correlation_id=correlation_id,
                    reply_to=callback_queue.name,
                    content_type="application/json",
                ),
                routing_key=self.requests_queue,
            )

            response = await asyncio.wait_for(future, timeout=300)
            if response.get("status") != "ok":
                raise RuntimeError(response.get("error", "Unknown imager error"))
            return response
        finally:
            await connection.close()

    async def list_groups(self) -> ListGroupsResponse:
        response = await self._rpc("list_groups")
        groups = [GroupInfo(**group) for group in response["data"]["groups"]]
        return ListGroupsResponse(groups=groups)

    async def generate_image(self, request: GenerateImageRequest) -> GenerateImageResponse:
        response = await self._rpc("generate_image", request.model_dump())
        return GenerateImageResponse(path=response["data"]["path"])

    async def sync_groups(self) -> SyncGroupsResponse:
        response = await self._rpc("sync_groups")
        return SyncGroupsResponse(message=response["data"]["message"])
