"""Validated editable presets; immutable render limits remain in the core."""
from decimal import Decimal, InvalidOperation
from enum import Enum

from pydantic import BaseModel, ValidationError, field_validator, model_validator

from imager_config.loader import config
from src.utils.building_blocks.result import Result


class ParamType(str, Enum):
    IMAGE_SIZE = 'image_size'
    NOISE_LEVEL = 'noise_level'
    INSET_SIZE = 'inset_size'
    IMAGE_GROUP = 'image_group'


def validate_display_name(value):
    if not isinstance(value, str):
        raise ValueError('Название должно быть текстом.')
    value = value.strip()
    if not value or len(value) > config.bot.ui_name_max_length:
        raise ValueError(f'Название должно содержать от 1 до {config.bot.ui_name_max_length} символов.')
    if any(ord(char) < 32 for char in value):
        raise ValueError('Название не должно содержать переводы строк и управляющие символы.')
    return value


class CommonParam(BaseModel):
    name: str
    value: int
    type: ParamType

    _validate_name = field_validator('name', mode='before')(validate_display_name)

    @field_validator('value', mode='before')
    @classmethod
    def integral_value(cls, value):
        if isinstance(value, bool):
            raise ValueError('Значение должно быть целым числом.')
        try:
            number = Decimal(str(value).strip().replace(',', '.'))
        except (InvalidOperation, ValueError):
            raise ValueError('Значение должно быть целым числом.') from None
        if not number.is_finite() or number != number.to_integral_value():
            raise ValueError('Значение должно быть конечным целым числом.')
        maximum = max(config.bot.result_size_max, config.bot.cell_size_max, config.bot.noise_max)
        if not 0 <= number <= maximum:
            raise ValueError(f'Значение должно находиться в диапазоне от 0 до {maximum}.')
        return int(number)

    @model_validator(mode='after')
    def validate_range(self):
        bounds = {
            ParamType.IMAGE_SIZE: (config.bot.result_size_min, config.bot.result_size_max),
            ParamType.NOISE_LEVEL: (config.bot.noise_min, config.bot.noise_max),
            ParamType.INSET_SIZE: (config.bot.cell_size_min, config.bot.cell_size_max),
        }
        if self.type not in bounds:
            raise ValueError('Этот тип не является числовым параметром.')
        minimum, maximum = bounds[self.type]
        if not minimum <= self.value <= maximum:
            raise ValueError(f'Допустимый диапазон: {minimum}–{maximum}.')
        return self

    @classmethod
    def create(cls, **kwargs) -> Result:
        try:
            return Result.Success(cls(**kwargs))
        except ValidationError as error:
            return Result.Error('; '.join(item['msg'] for item in error.errors()))


class ImageResultSize(CommonParam):
    type: ParamType = ParamType.IMAGE_SIZE


class NoiseLevel(CommonParam):
    type: ParamType = ParamType.NOISE_LEVEL


class InsetSize(CommonParam):
    type: ParamType = ParamType.INSET_SIZE
