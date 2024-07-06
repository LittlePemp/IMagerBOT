import cv2
from cv2.typing import MatLike
from src.shared_kernel.result import Result

from ..aggregates.cell import Cell
from ..errors.entities_errors import EntitiesErrorMessages
from ..value_objects.cell_rgb import CellRgb


class CellObject:
    def __init__(self, cell: Cell, image: MatLike) -> None:
        self.cell: Cell = cell
        self.image: MatLike = image

    @staticmethod
    def create(cell: Cell) -> Result:
        try:
            image: MatLike = cv2.imread(cell.relative_file_path)
            if image is None:
                return EntitiesErrorMessages.error_opening_image(
                    cell.relative_file_path)
            cell_object = CellObject(cell, image)
            return Result.Success(cell_object)
        except Exception as e:
            return EntitiesErrorMessages.error_opening_image(e)

    @staticmethod
    def create_from_image(rgb: CellRgb, group: str,
                          relative_file_path: str) -> Result:
        cell_result = Cell.create(rgb, group, relative_file_path)
        if not cell_result.is_success:
            return cell_result

        cell = cell_result.value
        image: MatLike = cv2.imread(relative_file_path)
        if image is None:
            return EntitiesErrorMessages.error_opening_image(
                relative_file_path)

        cell_object = CellObject(cell, image)
        return Result.Success(cell_object)

    def __str__(self):
        return str(self.cell)
