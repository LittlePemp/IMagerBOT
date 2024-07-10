import asyncio

from fastapi import APIRouter, HTTPException
from src.application.image_builder.mediator import mediator
from src.application.image_builder.queries.get_list_image_groups import \
    GetListImageGroupsQuery
from src.presentation.api.schemas import (GenerateImageRequest,
                                          GenerateImageResponse,
                                          ListGroupsResponse)
from src.shared_kernel.loggers import presentation_logger

router = APIRouter()

queue = asyncio.Queue()

@router.post("/generate-image", response_model=GenerateImageResponse)
async def generate_image(request: GenerateImageRequest):
    future = asyncio.Future()
    await queue.put({
        'image_path': request.image_path,
        'group_name': request.group_name,
        'insertion_format': request.insertion_format,
        'alpha_channel': request.alpha_channel,
        'noise_level': request.noise_level,
        'cell_size': request.cell_size,
        'result_size': request.result_size,
        'future': future
    })
    return await future

@router.get("/list-groups", response_model=ListGroupsResponse)
async def list_groups():
    try:
        query = GetListImageGroupsQuery()
        groups = await asyncio.to_thread(mediator.send, query)
        return ListGroupsResponse(groups=groups)
    except Exception as e:
        presentation_logger.error(f'An error occurred while listing groups: {e}')
        raise HTTPException(status_code=500, detail=str(e))
