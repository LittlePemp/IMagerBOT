import httpx
from src.infrastructure.services.imager_service.schemas import GenerateImageRequest, GenerateImageResponse, GroupInfo, ListGroupsResponse
from settings import settings

class ImagerService:
    def __init__(self, base_url: str):
        self.base_url = base_url

    async def list_groups(self) -> ListGroupsResponse:
        async with httpx.AsyncClient() as client:
            response = await client.get(f'{self.base_url}v1/list-groups')
            response.raise_for_status()
            groups_data = response.json()['groups']
            groups = [GroupInfo(**group) for group in groups_data]
            return ListGroupsResponse(groups=groups)

    async def generate_image(self, request: GenerateImageRequest) -> GenerateImageResponse:
        async with httpx.AsyncClient() as client:
            response = await client.post(f'{self.base_url}v1/generate-image', json=request.model_dump())
            response.raise_for_status()
            return GenerateImageResponse(path=response.json()['path'])
