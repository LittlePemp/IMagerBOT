import asyncio

from fastapi import HTTPException
from src.application.image_builder.commands.generate_image import \
    GenerateImageCommand
from src.application.image_builder.mediator import mediator
from src.presentation.api.routers import queue
from src.presentation.api.schemas import GenerateImageResponse
from src.shared_kernel.loggers import presentation_logger

semaphore = asyncio.Semaphore(2)

async def worker():
    while True:
        request = await queue.get()
        try:
            command = GenerateImageCommand(
                image_path=request['image_path'],
                group_name=request['group_name'],
                insertion_format=request['insertion_format'],
                alpha_channel=request['alpha_channel'],
                noise_level=request['noise_level'],
                cell_size=request['cell_size'],
                result_size=request['result_size']
            )
            async with semaphore:
                result = await asyncio.to_thread(mediator.send, command)
            if result.is_success:
                request['future'].set_result(GenerateImageResponse(path=result.value))
            else:
                request['future'].set_exception(HTTPException(status_code=400, detail=result.error))
        except Exception as e:
            presentation_logger.error(f'An error occurred while generating image: {e}')
            request['future'].set_exception(HTTPException(status_code=500, detail=str(e)))
        finally:
            queue.task_done()


def start_workers():
    for _ in range(2):
        asyncio.create_task(worker())
