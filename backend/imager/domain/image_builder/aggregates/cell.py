from eventsourcing.domain import Aggregate, event

from ....shared_kernel.result import Result
from ..value_objects.cell_rgb import CellRgb


class Cell(Aggregate):
    def __init__(self, rgb: CellRgb, group: str, relative_file_path: str):
        super().__init__()
        self.rgb: CellRgb = rgb
        self.group: str = group
        self.relative_file_path: str = relative_file_path

    @staticmethod
    def create(rgb: CellRgb, group: str, relative_file_path: str) -> Result:
        cell: Cell = Cell(rgb, group, relative_file_path)
        return Result.Success(cell)

    @event('CellUpdated')
    def update_cell(self, rgb: CellRgb, group: str, relative_file_path: str):
        self.rgb = rgb
        self.group = group
        self.relative_file_path = relative_file_path

    def __str__(self):
        return (
            'Cell INFO: '
            f'{self.relative_file_path}; '
            f'{self.group}; '
            f'{self.rgb}; '
        )
