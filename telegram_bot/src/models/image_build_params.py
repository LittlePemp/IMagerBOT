from enum import Enum
from pydantic import BaseModel, ValidationError
from src.utils.building_blocks.result import Result

class ParamType(str, Enum):
    IMAGE_SIZE = 'image_size'
    NOISE_LEVEL = 'noise_level'
    INSET_SIZE = 'inset_size'
    IMAGE_GROUP = 'image_group'


class CommonParam(BaseModel):
    name: str
    value: float
    type: ParamType

    @classmethod
    def create(cls, **kwargs) -> Result:
        try:
            param = cls(**kwargs)
            return Result.Success(param)
        except ValidationError as e:
            return Result.Error(f'Validation error: {e}')


class ImageResultSize(CommonParam):
    type: ParamType = ParamType.IMAGE_SIZE


class NoiseLevel(CommonParam):
    type: ParamType = ParamType.NOISE_LEVEL


class InsetSize(CommonParam):
    type: ParamType = ParamType.INSET_SIZE
