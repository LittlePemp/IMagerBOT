from src.shared_kernel.result import Result

from ..errors.entities_errors import EntitiesErrorMessages


class ImageInsertionFormat:
    CROP = 'crop'
    SCALE = 'scale'

    def __init__(self, value):
        self.value = value

    @staticmethod
    def create(value) -> Result:
        if (not isinstance(value, str)
                or value not in [ImageInsertionFormat.CROP,
                                 ImageInsertionFormat.SCALE]):
            return EntitiesErrorMessages.invalid_insertion_format()
        return Result.Success(ImageInsertionFormat(value))

    def __str__(self):
        return self.value


class AlphaChannel:
    def __init__(self, value):
        self.value = value

    @staticmethod
    def create(value) -> Result:
        if type(value) is not int:
            return EntitiesErrorMessages.alpha_channel_must_be_an_integer()
        if not (0 <= value <= 100):
            return EntitiesErrorMessages.alpha_channel_must_be_in_range()
        return Result.Success(AlphaChannel(value))

    def __int__(self):
        return self.value

    def __str__(self):
        return str(self.value)


class NoiseLevel:
    def __init__(self, value):
        self.value = value

    @staticmethod
    def create(value) -> Result:
        if type(value) is not int:
            return EntitiesErrorMessages.noise_level_must_be_an_integer()
        if not (0 <= value <= 100):
            return EntitiesErrorMessages.noise_level_must_be_in_range()
        return Result.Success(NoiseLevel(value))

    def __int__(self):
        return self.value

    def __str__(self):
        return str(self.value)


class CellSize:
    MAX_VALUE = 2000

    def __init__(self, value):
        self.value = value

    @staticmethod
    def create(value) -> Result:
        if type(value) is not int:
            return EntitiesErrorMessages.cell_size_must_be_an_integer()
        if value <= 0:
            return EntitiesErrorMessages.cell_size_must_be_greater_than_zero()
        if value > CellSize.MAX_VALUE:
            return EntitiesErrorMessages.cell_size_exceeds_limit(CellSize.MAX_VALUE)
        return Result.Success(CellSize(value))

    def __int__(self):
        return self.value

    def __str__(self):
        return str(self.value)


class ResultSize:
    MAX_VALUE = 1000

    def __init__(self, value):
        self.value = value

    @staticmethod
    def create(value) -> Result:
        if type(value) is not int:
            return EntitiesErrorMessages.result_size_must_be_an_integer()
        if value <= 0:
            return EntitiesErrorMessages.result_size_must_be_greater_than_zero()  # noqa
        if value > ResultSize.MAX_VALUE:
            return EntitiesErrorMessages.result_size_exceeds_limit(ResultSize.MAX_VALUE)
        return Result.Success(ResultSize(value))

    def __int__(self):
        return self.value

    def __str__(self):
        return str(self.value)


class ImageGroup:
    def __init__(self, value):
        self.value = value

    @staticmethod
    def create(value) -> Result:
        if not isinstance(value, str):
            return EntitiesErrorMessages.image_group_must_be_a_string()
        if not value.strip():
            return EntitiesErrorMessages.image_group_cannot_be_empty()
        return Result.Success(ImageGroup(value))

    def __str__(self):
        return self.value
