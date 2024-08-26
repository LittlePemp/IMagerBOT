import httpx
from pydantic import ValidationError
from src.infrastructure.services.imager_service.schemas import (
    GenerateImageRequest, GenerateImageResponse, GroupInfo, ListGroupsResponse)
from src.utils.building_blocks.result import Result
from src.utils.loggers import exception_logger


class ImagerService:
    def __init__(self, base_url: str, timeout: float = 10.0):
        self.base_url = base_url
        self.timeout = timeout

    async def list_groups(self) -> Result:
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(f'{self.base_url}v1/list-groups')
                response.raise_for_status()
                groups_data = response.json().get('groups', [])
                groups = [GroupInfo(**group) for group in groups_data]
                return Result.Success(ListGroupsResponse(groups=groups))
        except (httpx.HTTPStatusError, httpx.RequestError) as e:
            exception_logger.error(f'Failed to list groups: {e}')
            return Result.Error(f'HTTP error: {e}')
        except ValidationError as e:
            exception_logger.error(f'Validation error when parsing groups: {e}')
            return Result.Error(f'Validation error: {e}')

    async def generate_image(self, request: GenerateImageRequest) -> Result:
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(f'{self.base_url}v1/generate-image', json=request.model_dump())
                response.raise_for_status()
                return Result.Success(GenerateImageResponse(path=response.json()['path']))
        except (httpx.HTTPStatusError, httpx.RequestError) as e:
            exception_logger.error(f'Failed to generate image: {e}')
            return Result.Error(f'HTTP error: {e}')
        except ValidationError as e:
            exception_logger.error(f'Validation error when parsing response: {e}')
            return Result.Error(f'Validation error: {e}')
