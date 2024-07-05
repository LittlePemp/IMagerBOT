from src.shared_kernel.result import Result


class ImageInsertionFormat:
    CROP = 'crop'
    SCALE = 'scale'

    def __init__(self, value):
        self.value = value

    @staticmethod
    def create(value) -> Result:
        if not isinstance(value, str) or value not in [ImageInsertionFormat.CROP, ImageInsertionFormat.SCALE]:
            return Result.Error('Invalid insertion format')
        return Result.Success(ImageInsertionFormat(value))

    def __str__(self):
        return self.value


class AlphaChannel:
    def __init__(self, value):
        self.value = value

    @staticmethod
    def create(value) -> Result:
        if not isinstance(value, int):
            return Result.Error('Alpha channel must be an integer')
        if not (0 <= value <= 100):
            return Result.Error('Alpha channel must be between 0 and 100')
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
        if not isinstance(value, int):
            return Result.Error('Noise level must be an integer')
        if not (0 <= value <= 100):
            return Result.Error('Noise level must be between 0 and 100')
        return Result.Success(NoiseLevel(value))

    def __int__(self):
        return self.value

    def __str__(self):
        return str(self.value)


class CellSize:
    def __init__(self, value):
        self.value = value

    @staticmethod
    def create(value) -> Result:
        if not isinstance(value, int):
            return Result.Error('Cell size must be an integer')
        if value <= 0:
            return Result.Error('Cell size must be greater than 0')
        return Result.Success(CellSize(value))

    def __int__(self):
        return self.value

    def __str__(self):
        return str(self.value)


class ResultSize:
    def __init__(self, value):
        self.value = value

    @staticmethod
    def create(value) -> Result:
        if not isinstance(value, int):
            return Result.Error('Result size must be an integer')
        if value <= 0:
            return Result.Error('Result size must be greater than 0')
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
            return Result.Error('Image group must be a string')
        if not value:
            return Result.Error('Image group cannot be empty')
        return Result.Success(ImageGroup(value))

    def __str__(self):
        return self.value
