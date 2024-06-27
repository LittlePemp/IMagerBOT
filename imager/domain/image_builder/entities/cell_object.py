import cv2
from cv2.typing import MatLike
from imager.domain.image_builder.aggregates.cell import Cell
from imager.shared_kernel.result import Result


class CellObject:
    def __init__(self, cell: Cell, image: MatLike) -> None:
        self.cell: Cell = cell
        self.image: MatLike = image

    @staticmethod
    def create(cell: Cell) -> Result:
        try:
            image: MatLike = cv2.imread(cell.relative_file_path)
            cell_object = CellObject(cell, image)
            return Result.Success(cell_object)
        except Exception as e:
            return Result.Error(f'Error opening image: {e}')

    def __str__(self):
        return str(self.cell)
