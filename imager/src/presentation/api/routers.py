from fastapi import APIRouter, HTTPException
from src.application.image_builder.commands.generate_image import \
    GenerateImageCommand
from src.application.image_builder.mediator import mediator
from src.presentation.api.schemas import (GenerateImageRequest,
                                          GenerateImageResponse)
from src.shared_kernel.loggers import presentation_logger

router = APIRouter()


@router.post("/generate-image", response_model=GenerateImageResponse)
async def generate_image(request: GenerateImageRequest):
    try:
        command = GenerateImageCommand(
            image_path=request.image_path,
            group_name=request.group_name,
            insertion_format=request.insertion_format,
            alpha_channel=request.alpha_channel,
            noise_level=request.noise_level,
            cell_size=request.cell_size,
            result_size=request.result_size
        )
        result = mediator.send(command)
        if result.is_success:
            return GenerateImageResponse(path=result.value)
        else:
            raise HTTPException(status_code=400, detail=result.error)
    except Exception as e:
        presentation_logger.error(f'An error occurred while generating image: {e}')
        raise HTTPException(status_code=500, detail=str(e))
