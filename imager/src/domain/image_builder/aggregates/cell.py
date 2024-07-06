from eventsourcing.domain import Aggregate
from src.domain.image_builder.value_objects.cell_rgb import CellRgb
from src.shared_kernel.result import Result

from ..errors.entities_errors import EntitiesErrorMessages


class Cell(Aggregate):
    def __init__(self, rgb: CellRgb, group: str, relative_file_path: str):
        super().__init__()
        self.rgb: CellRgb = rgb
        self.group: str = group
        self.relative_file_path: str = relative_file_path

    @staticmethod
    def create(rgb: CellRgb, group: str, relative_file_path: str) -> Result:
        if not isinstance(group, str) or not group.strip():
            return EntitiesErrorMessages.group_must_be_non_empty_string()
        if (not isinstance(relative_file_path, str)
                or not relative_file_path.strip()):
            return EntitiesErrorMessages.relative_file_path_must_be_non_empty_string()  # noqa

        cell: Cell = Cell(rgb, group, relative_file_path)
        return Result.Success(cell)

    def __str__(self):
        return (
            'Cell INFO: '
            f'{self.relative_file_path}; '
            f'{self.group}; '
            f'{self.rgb}; '
        )
