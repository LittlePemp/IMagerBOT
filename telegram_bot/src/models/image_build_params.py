from pydantic import BaseModel, ValidationError
from src.utils.building_blocks.result import Result

class ImageSize(BaseModel):
    name: str
    value: int

    @classmethod
    def create(cls, **kwargs) -> Result:
        try:
            valid_fields = cls.model_fields.keys()
            filtered_kwargs = {k: v for k, v in kwargs.items() if k in valid_fields}
            image_size = cls(**filtered_kwargs)
            return Result.Success(image_size)
        except ValidationError as e:
            return Result.Error(f'Validation error: {e}')


class NoiseLevel(BaseModel):
    name: str
    value: int

    @classmethod
    def create(cls, **kwargs) -> Result:
        try:
            valid_fields = cls.model_fields.keys()
            filtered_kwargs = {k: v for k, v in kwargs.items() if k in valid_fields}
            noise_level = cls(**filtered_kwargs)
            return Result.Success(noise_level)
        except ValidationError as e:
            return Result.Error(f'Validation error: {e}')


class InsetSize(BaseModel):
    name: str
    value: int

    @classmethod
    def create(cls, **kwargs) -> Result:
        try:
            valid_fields = cls.model_fields.keys()
            filtered_kwargs = {k: v for k, v in kwargs.items() if k in valid_fields}
            inset_size = cls(**filtered_kwargs)
            return Result.Success(inset_size)
        except ValidationError as e:
            return Result.Error(f'Validation error: {e}')


class ImageType(BaseModel):
    name: str
    group: str
    active: bool

    @classmethod
    def create(cls, **kwargs) -> Result:
        try:
            valid_fields = cls.model_fields.keys()
            filtered_kwargs = {k: v for k, v in kwargs.items() if k in valid_fields}
            image_type = cls(**filtered_kwargs)
            return Result.Success(image_type)
        except ValidationError as e:
            return Result.Error(f'Validation error: {e}')
