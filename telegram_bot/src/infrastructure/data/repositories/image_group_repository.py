from typing import Optional

from src.infrastructure.data.models.image_group import ImageGroup


class ImageGroupRepository:
    def __init__(self, db):
        self.collection = db['image_groups']

    async def add_group(self, group: ImageGroup):
        await self.collection.insert_one(group.model_dump())

    async def get_group_by_name(self, imager_name: str) -> Optional[ImageGroup]:
        group_data = await self.collection.find_one({'imager_name': imager_name})
        if group_data:
            return ImageGroup(**group_data)
        return None

    async def update_group(self, imager_name: str, update_data: dict):
        await self.collection.update_one({'imager_name': imager_name}, {'$set': update_data})
