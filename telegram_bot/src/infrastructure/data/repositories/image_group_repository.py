from datetime import datetime
from motor.motor_asyncio import AsyncIOMotorCollection

from settings import settings
from src.models.image_group import ImageGroup, GroupStatus


class ImageGroupRepository:
    _instance = None

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super(ImageGroupRepository, cls).__new__(cls)
        return cls._instance

    def __init__(self, collection: AsyncIOMotorCollection):
        if hasattr(self, '_initialized') and self._initialized:
            return
        self.collection = collection
        self._initialized = True

    async def sync_groups(self, groups: list[ImageGroup]):
        current_groups = await self.collection.find().to_list(length=None)
        current_group_names = {group['imager_name'] for group in current_groups}

        for group in groups:
            existing_group = next((g for g in current_groups if g['imager_name'] == group.imager_name), None)
            if existing_group:
                await self.collection.update_one(
                    {'imager_name': group.imager_name},
                    {'$set': {
                        'count': group.count,
                        'last_synced': datetime.now(settings.tzinfo),
                        'status': GroupStatus.ACTIVE
                    }}
                )
            else:
                await self.collection.insert_one(group.model_dump())
            current_group_names.discard(group.imager_name)

        if current_group_names:
            await self.collection.update_many(
                {'imager_name': {'$in': list(current_group_names)}},
                {'$set': {'status': GroupStatus.INACTIVE, 'last_synced': datetime.now(settings.tzinfo)}}
            )

    async def get_groups(self, status: GroupStatus = GroupStatus.ACTIVE) -> list[ImageGroup]:
        groups_data = await self.collection.find({'status': status}).to_list(length=None)
        return [ImageGroup(**group) for group in groups_data]

    async def update_group_name(self, old_name: str, new_name: str):
        result = await self.collection.update_one(
            {'imager_name': old_name},
            {'$set': {'imager_name': new_name, 'last_synced': datetime.now(settings.tzinfo)}}
        )
        if result.matched_count == 0:
            raise ValueError(f'Group with name {old_name} not found')
