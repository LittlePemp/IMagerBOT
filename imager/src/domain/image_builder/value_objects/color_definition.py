from src.shared_kernel.result import Result

from ..errors.entities_errors import EntitiesErrorMessages


class ColorDefinition:
    def __init__(self, value: int):
        self.value: int = value

    @staticmethod
    def create(value: int) -> Result:
        if not isinstance(value, int):
            return EntitiesErrorMessages.rgb_must_be_an_integer()

        if not (0 <= value <= 255):
            return EntitiesErrorMessages.rgb_must_be_in_range()

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
