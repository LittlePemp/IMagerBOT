from datetime import datetime
from typing import Optional
from motor.motor_asyncio import AsyncIOMotorCollection

from settings import settings
from src.models.image_group import ImageGroup, GroupStatus
from src.infrastructure.services.imager_service.schemas import GroupInfo
from src.utils.loggers import exception_logger
from src.utils.building_blocks.result import Result


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

    async def sync_groups(self, groups: list[GroupInfo]):
        current_groups = await self.collection.find().to_list(length=None)
        current_group_names = {group['imager_name'] for group in current_groups}

        for group in groups:
            existing_group = next((g for g in current_groups if g['imager_name'] == group.group), None)
            if existing_group:
                await self.collection.update_one(
                    {'imager_name': group.group},
                    {'$set': {
                        'count': group.count,
                        'last_synced': datetime.now(settings.tzinfo),
                        'status': GroupStatus.ACTIVE
                    }}
                )
            else:
                new_group = ImageGroup(
                    imager_name=group.group,
                    display_name=group.group,
                    count=group.count,
                    last_synced=datetime.now(settings.tzinfo)
                )
                await self.collection.insert_one(new_group.model_dump())
            current_group_names.discard(group.group)

        if current_group_names:
            await self.collection.update_many(
                {'imager_name': {'$in': list(current_group_names)}},
                {'$set': {'status': GroupStatus.INACTIVE, 'last_synced': datetime.now(settings.tzinfo)}}
            )

    async def get_groups(self, status: GroupStatus = GroupStatus.ACTIVE) -> Result:
        try:
            groups_data = await self.collection.find({'status': status}).to_list(length=None)
            groups = [ImageGroup(**group) for group in groups_data]
            return Result.Success(groups)
        except Exception as e:
            exception_logger.error(f'Failed to get groups with status {status}: {e}')
            return Result.Error(f'Failed to get groups: {e}')

    async def get_group_by_name(self, imager_name: str) -> ImageGroup:
        group_data = await self.collection.find_one({'imager_name': imager_name})
        if group_data:
            return ImageGroup(**group_data)
        return None

    async def update_group_name(self, imager_name: str, new_display_name: str):
        result = await self.collection.update_one(
            {'imager_name': imager_name},
            {'$set': {'display_name': new_display_name, 'last_synced': datetime.now(settings.tzinfo)}}
        )
        if result.matched_count == 0:
            raise ValueError(f'Group with imager_name {imager_name} not found')

    async def update_group_status(self, imager_name: str, status: GroupStatus):
        result = await self.collection.update_one(
            {'imager_name': imager_name},
            {'$set': {'status': status, 'last_synced': datetime.now(settings.tzinfo)}}
        )
        if result.matched_count == 0:
            raise ValueError(f'Group with imager_name {imager_name} not found')
