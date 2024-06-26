from .color_definition import ColorDefinition
from src.shared_kernel.result import Result


class CellRgb:
    def __init__(
        self, r: ColorDefinition, g: ColorDefinition, b: ColorDefinition
    ):
        self.r: ColorDefinition = r
        self.g: ColorDefinition = g
        self.b: ColorDefinition = b

    @staticmethod
    def create(r, g, b) -> Result:
        red_result: Result = ColorDefinition.create(r)
        if not red_result.is_success:
            return Result.Error(red_result.error)

        green_result: Result = ColorDefinition.create(g)
        if not green_result.is_success:
            return Result.Error(green_result.error)

        blue_result: Result = ColorDefinition.create(b)
        if not blue_result.is_success:
            return Result.Error(blue_result.error)

        cell_rgb: CellRgb = CellRgb(
            red_result.value,
            green_result.value,
            blue_result.value,
        )

        return Result.Success(cell_rgb)

    def __str__(self):
        return (
            f'Red: {self.r}, '
            f'Green: {self.g}, '
            f'Blue: {self.b}.'
        )
