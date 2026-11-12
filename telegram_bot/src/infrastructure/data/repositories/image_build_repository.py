from bson import ObjectId
from pydantic import ValidationError
from pymongo.asynchronous.collection import AsyncCollection

from src.models.image_build_params import CommonParam, ParamType
from src.utils.building_blocks.result import Result
from src.utils.loggers import exception_logger


class ImageBuildParamsRepository:
    def __init__(self, collection: AsyncCollection):
        self.collection = collection

    async def initialize_indexes(self):
        await self.collection.create_index(
            [('type', 1), ('name', 1)], unique=True,
            partialFilterExpression={
                'type': {'$in': [ParamType.IMAGE_SIZE.value, ParamType.NOISE_LEVEL.value, ParamType.INSET_SIZE.value]},
                'name': {'$type': 'string'},
            },
        )

    async def add_common_param(self, param: CommonParam) -> Result:
        validated = CommonParam.model_validate(param.model_dump())
        try:
            await self.collection.insert_one(validated.model_dump())
            return Result.Success(validated)
        except Exception:
            exception_logger.exception('Cannot save generation preset')
            return Result.Error('Не удалось сохранить параметр. Возможно, такое название уже существует.')

    async def get_common_params(self, param_type: ParamType) -> Result:
        documents = await self.collection.find({'type': param_type}).sort('name', 1).to_list(length=None)
        params = []
        for document in documents:
            try:
                params.append(CommonParam(**document))
            except ValidationError:
                exception_logger.warning(f'Invalid legacy preset excluded from selection: id={document.get("_id")}')
        return Result.Success(params)

    async def get_admin_params(self, param_type: ParamType):
        """Include invalid legacy rows so an administrator can remove them."""
        rows = await self.collection.find({'type': param_type}).sort('name', 1).to_list(length=None)
        return [dict(row, record_id=str(row['_id'])) for row in rows]

    async def delete_common_param(self, param_type: ParamType, param_name: str) -> Result:
        result = await self.collection.delete_one({'type': param_type, 'name': param_name})
        return Result.Success(None) if result.deleted_count else Result.Error('Параметр уже удалён.')

    async def delete_by_id(self, record_id: str, param_type: ParamType) -> bool:
        result = await self.collection.delete_one({'_id': ObjectId(record_id), 'type': param_type})
        return bool(result.deleted_count)
