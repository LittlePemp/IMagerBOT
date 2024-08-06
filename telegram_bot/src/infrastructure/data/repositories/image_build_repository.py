from motor.motor_asyncio import AsyncIOMotorCollection
from typing import List
from src.models.image_build_params import CommonParam, ImageGroup, ParamType
from src.utils.loggers import db_logger, exception_logger
from src.utils.building_blocks.result import Result

class ImageBuildParamsRepository:
    _instance = None

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super(ImageBuildParamsRepository, cls).__new__(cls)
        return cls._instance

    def __init__(self, collection: AsyncIOMotorCollection):
        if hasattr(self, '_initialized') and self._initialized:
            return
        self.collection = collection
        self._initialized = True

    async def add_common_param(self, param: CommonParam) -> Result:
        try:
            await self.collection.insert_one(param.model_dump())
            db_logger.info(f'Param added: {param}')
            return Result.Success(param)
        except Exception as e:
            exception_logger.error(f'Failed to add param: {e}')
            return Result.Error(f'Failed to add param: {e}')

    async def get_common_params(self, param_type: ParamType) -> Result:
        try:
            params = await self.collection.find({'type': param_type}).to_list(length=None)
            return Result.Success([CommonParam(**param) for param in params])
        except Exception as e:
            exception_logger.error(f'Failed to get params: {e}')
            return Result.Error(f'Failed to get params: {e}')

    async def delete_common_param(self, param_type: ParamType, param_name: str) -> Result:
        try:
            result = await self.collection.delete_one({'type': param_type, 'name': param_name})
            if result.deleted_count == 0:
                return Result.Error(f'Param with name {param_name} not found')
            db_logger.info(f'Param deleted: {param_name}')
            return Result.Success(None)
        except Exception as e:
            exception_logger.error(f'Failed to delete param: {e}')
            return Result.Error(f'Failed to delete param: {e}')

    async def add_image_group(self, image_group: ImageGroup) -> Result:
        try:
            await self.collection.insert_one(image_group.model_dump())
            db_logger.info(f'Image group added: {image_group}')
            return Result.Success(image_group)
        except Exception as e:
            exception_logger.error(f'Failed to add image group: {e}')
            return Result.Error(f'Failed to add image group: {e}')

    async def get_image_groups(self) -> Result:
        try:
            groups = await self.collection.find({'type': ParamType.IMAGE_GROUP}).to_list(length=None)
            return Result.Success([ImageGroup(**group) for group in groups])
        except Exception as e:
            exception_logger.error(f'Failed to get image groups: {e}')
            return Result.Error(f'Failed to get image groups: {e}')

    async def update_image_group_status(self, name: str, active: bool) -> Result:
        try:
            result = await self.collection.update_one({'type': ParamType.IMAGE_GROUP, 'name': name}, {'$set': {'active': active}})
            if result.matched_count == 0:
                return Result.Error(f'Image group with name {name} not found')
            db_logger.info(f'Image group status updated: {name} to {"active" if active else "inactive"}')
            return Result.Success(None)
        except Exception as e:
            exception_logger.error(f'Failed to update image group status: {e}')
            return Result.Error(f'Failed to update image group status: {e}')

    async def rename_image_group(self, old_name: str, new_name: str) -> Result:
        try:
            result = await self.collection.update_one({'type': ParamType.IMAGE_GROUP, 'name': old_name}, {'$set': {'name': new_name}})
            if result.matched_count == 0:
                return Result.Error(f'Image group with name {old_name} not found')
            db_logger.info(f'Image group renamed from {old_name} to {new_name}')
            return Result.Success(None)
        except Exception as e:
            exception_logger.error(f'Failed to rename image group: {e}')
            return Result.Error(f'Failed to rename image group: {e}')
