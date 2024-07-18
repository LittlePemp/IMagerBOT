from typing import Optional

from src.infrastructure.data.models.generated_image import GeneratedImage


class GeneratedImageRepository:
    def __init__(self, db):
        self.collection = db['generated_images']

    async def add_generated_image(self, image: GeneratedImage):
        await self.collection.insert_one(image.model_dump())

    async def get_image_by_path(self, generated_path: str) -> Optional[GeneratedImage]:
        image_data = await self.collection.find_one({'generated_path': generated_path})
        if image_data:
            return GeneratedImage(**image_data)
        return None
