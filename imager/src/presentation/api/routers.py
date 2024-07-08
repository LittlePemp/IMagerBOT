import asyncio

from fastapi import APIRouter
from src.presentation.api.schemas import (GenerateImageRequest,
                                          GenerateImageResponse)

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
