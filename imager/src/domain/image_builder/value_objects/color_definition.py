from src.shared_kernel.result import Result


class ColorDefinition:
    def __init__(self, value: int):
        self.value: int = value

    @staticmethod
    def create(value: int) -> Result:
        if not isinstance(value, int):
            return Result.Error('RGB must be an integer')

        if not (0 <= value <= 255):
            return Result.Error('RGB must be in [0..255]')

        return Result.Success(ColorDefinition(value))

    def __int__(self):
        return self.value

    def __str__(self):
        return str(self.value)

    def __lt__(self, other):
        if isinstance(other, ColorDefinition):
            return self.value < other.value
        return NotImplemented

    def __gt__(self, other):
        if isinstance(other, ColorDefinition):
            return self.value > other.value
        return NotImplemented

    def __eq__(self, other):
        if isinstance(other, ColorDefinition):
            return self.value == other.value
        return NotImplemented
