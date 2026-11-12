import asyncio
import uuid

from fastapi import APIRouter, HTTPException
from settings import settings
from src.application.image_builder.commands.load_missing_groups import \
    LoadMissingGroupsCommand
from src.application.image_builder.mediator import mediator
from src.application.image_builder.queries.get_list_image_groups import \
    GetListImageGroupsQuery
from src.presentation.api.schemas import (GenerateImageRequest,
                                          GenerateImageResponse,
                                          ListGroupsResponse,
                                          SyncGroupsResponse)
from imager_config.loader import config
from src.shared_kernel.loggers import presentation_logger
from src.presentation.api.workers import accepting_requests, generation_queue

router = APIRouter()

@router.post("/v1/generate-image", response_model=GenerateImageResponse)
async def generate_image(request: GenerateImageRequest):
    if not accepting_requests():
        raise HTTPException(status_code=503, detail='Generator is not accepting requests')
    queue = generation_queue()
    future = asyncio.get_running_loop().create_future()
    request_id = str(uuid.uuid4())
    item = {
        'image_path': request.image_path,
        'group_name': request.group_name,
        'insertion_format': request.insertion_format,
        'alpha_channel': request.alpha_channel,
        'noise_level': request.noise_level,
        'cell_size': request.cell_size,
        'result_size': request.result_size,
        'output_name': request.output_name,
        'future': future,
        'request_id': request_id,
    }
    try:
        queue.put_nowait(item)
    except asyncio.QueueFull:
        raise HTTPException(status_code=429, detail='Generation queue is full; try again later')
    try:
        return await asyncio.wait_for(
            asyncio.shield(future),
            timeout=getattr(settings, 'api_request_timeout_seconds', config.legacy.api_request_timeout_seconds),
        )
    except asyncio.TimeoutError:
        future.cancel()
        presentation_logger.warning(f'HTTP generation wait timed out: request_id={request_id}')
        raise HTTPException(
            status_code=504,
            detail='Generation wait timed out; a calculation already started may still complete',
        )
    except asyncio.CancelledError:
        future.cancel()
        raise

@router.get("/v1/list-groups", response_model=ListGroupsResponse)
async def list_groups():
    try:
        query = GetListImageGroupsQuery()
        groups_info = await asyncio.to_thread(mediator.send, query)
        return ListGroupsResponse(groups=groups_info)
    except Exception as e:
        presentation_logger.error(f'An error occurred while listing groups: {e}')
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/v1/sync-groups", response_model=SyncGroupsResponse)
async def sync_groups():
    try:
        command = LoadMissingGroupsCommand()
        result = await asyncio.to_thread(mediator.send, command)
        if not result.is_success:
            raise HTTPException(status_code=400, detail=result.error)
        return SyncGroupsResponse(message=result.value)
    except HTTPException:
        raise
    except Exception as e:
        presentation_logger.error(f'An error occurred while syncing groups: {e}')
        raise HTTPException(status_code=500, detail=str(e))
