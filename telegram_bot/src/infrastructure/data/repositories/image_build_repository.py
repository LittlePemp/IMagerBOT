from motor.motor_asyncio import AsyncIOMotorCollection
from typing import List
from src.models.image_build_params import ImageSize, NoiseLevel, InsetSize, ImageType
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

    async def add_image_size(self, size: ImageSize) -> Result:
        try:
            await self.collection.insert_one(size.dict())
            db_logger.info(f'Image size added: {size}')
            return Result.Success(size)
        except Exception as e:
            exception_logger.error(f'Failed to add image size: {e}')
            return Result.Error(f'Failed to add image size: {e}')

    async def get_image_sizes(self) -> Result:
        try:
            sizes = await self.collection.find({'type': 'image_size'}).to_list(length=None)
            return Result.Success([ImageSize(**size) for size in sizes])
        except Exception as e:
            exception_logger.error(f'Failed to get image sizes: {e}')
            return Result.Error(f'Failed to get image sizes: {e}')

    async def delete_image_size(self, name: str) -> Result:
        try:
            result = await self.collection.delete_one({'type': 'image_size', 'name': name})
            if result.deleted_count == 0:
                return Result.Error(f'Image size with name {name} not found')
            db_logger.info(f'Image size deleted: {name}')
            return Result.Success(None)
        except Exception as e:
            exception_logger.error(f'Failed to delete image size: {e}')
            return Result.Error(f'Failed to delete image size: {e}')

    async def add_noise_level(self, noise: NoiseLevel) -> Result:
        try:
            await self.collection.insert_one(noise.dict())
            db_logger.info(f'Noise level added: {noise}')
            return Result.Success(noise)
        except Exception as e:
            exception_logger.error(f'Failed to add noise level: {e}')
            return Result.Error(f'Failed to add noise level: {e}')

    async def get_noise_levels(self) -> Result:
        try:
            noises = await self.collection.find({'type': 'noise_level'}).to_list(length=None)
            return Result.Success([NoiseLevel(**noise) for noise in noises])
        except Exception as e:
            exception_logger.error(f'Failed to get noise levels: {e}')
            return Result.Error(f'Failed to get noise levels: {e}')

    async def delete_noise_level(self, name: str) -> Result:
        try:
            result = await self.collection.delete_one({'type': 'noise_level', 'name': name})
            if result.deleted_count == 0:
                return Result.Error(f'Noise level with name {name} not found')
            db_logger.info(f'Noise level deleted: {name}')
            return Result.Success(None)
        except Exception as e:
            exception_logger.error(f'Failed to delete noise level: {e}')
            return Result.Error(f'Failed to delete noise level: {e}')

    async def add_inset_size(self, inset: InsetSize) -> Result:
        try:
            await self.collection.insert_one(inset.dict())
            db_logger.info(f'Inset size added: {inset}')
            return Result.Success(inset)
        except Exception as e:
            exception_logger.error(f'Failed to add inset size: {e}')
            return Result.Error(f'Failed to add inset size: {e}')

    async def get_inset_sizes(self) -> Result:
        try:
            insets = await self.collection.find({'type': 'inset_size'}).to_list(length=None)
            return Result.Success([InsetSize(**inset) for inset in insets])
        except Exception as e:
            exception_logger.error(f'Failed to get inset sizes: {e}')
            return Result.Error(f'Failed to get inset sizes: {e}')

    async def delete_inset_size(self, name: str) -> Result:
        try:
            result = await self.collection.delete_one({'type': 'inset_size', 'name': name})
            if result.deleted_count == 0:
                return Result.Error(f'Inset size with name {name} not found')
            db_logger.info(f'Inset size deleted: {name}')
            return Result.Success(None)
        except Exception as e:
            exception_logger.error(f'Failed to delete inset size: {e}')
            return Result.Error(f'Failed to delete inset size: {e}')

    async def add_image_type(self, image_type: ImageType) -> Result:
        try:
            await self.collection.insert_one(image_type.dict())
            db_logger.info(f'Image type added: {image_type}')
            return Result.Success(image_type)
        except Exception as e:
            exception_logger.error(f'Failed to add image type: {e}')
            return Result.Error(f'Failed to add image type: {e}')

    async def get_image_types(self) -> Result:
        try:
            types = await self.collection.find({'type': 'image_type'}).to_list(length=None)
            return Result.Success([ImageType(**type) for type in types])
        except Exception as e:
            exception_logger.error(f'Failed to get image types: {e}')
            return Result.Error(f'Failed to get image types: {e}')

    async def update_image_type_status(self, name: str, active: bool) -> Result:
        try:
            result = await self.collection.update_one({'type': 'image_type', 'name': name}, {'$set': {'active': active}})
            if result.matched_count == 0:
                return Result.Error(f'Image type with name {name} not found')
            db_logger.info(f'Image type status updated: {name} to {"active" if active else "inactive"}')
            return Result.Success(None)
        except Exception as e:
            exception_logger.error(f'Failed to update image type status: {e}')
            return Result.Error(f'Failed to update image type status: {e}')

    async def rename_image_type(self, old_name: str, new_name: str) -> Result:
        try:
            result = await self.collection.update_one({'type': 'image_type', 'name': old_name}, {'$set': {'name': new_name}})
            if result.matched_count == 0:
                return Result.Error(f'Image type with name {old_name} not found')
            db_logger.info(f'Image type renamed from {old_name} to {new_name}')
            return Result.Success(None)
        except Exception as e:
            exception_logger.error(f'Failed to rename image type: {e}')
            return Result.Error(f'Failed to rename image type: {e}')
