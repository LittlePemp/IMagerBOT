import os
import cv2
import numpy as np
from imager.shared_kernel.result import Result
from imager.shared_kernel.loggers import db_logger
from imager.domain.image_builder.services.image_service import ImageService
from settings import settings


class FileRepository:
    def __init__(self):
        self.settings = settings
        self.image_service = ImageService()

    def read_image_file(self, relative_path: str) -> Result:
        image = cv2.imread(relative_path, cv2.IMREAD_UNCHANGED)
        if image is None:
            return Result.Error(f"Image not found at path: {relative_path}")
        return Result.Success(image)

    def save_image_file(self, image: np.ndarray, relative_path: str) -> None:
        cv2.imwrite(relative_path, image)
        db_logger.info(f'Image saved at {relative_path}')

    def list_image_files(self, directory_path: str) -> list[str]:
        supported_extensions = ('.png', '.jpg', '.jpeg', '.bmp', '.tiff')
        return [
            os.path.join(directory_path, f)
            for f in os.listdir(directory_path)
            if f.lower().endswith(supported_extensions)
        ]

    def load_image(self, image_path: str, group_name: str) -> Result:
        image_result = self.read_image_file(image_path)
        if not image_result.is_success:
            return Result.Error(image_result.error)

        image = image_result.value
        avg_color = self.image_service.average_color(image)

        cell_data = {
            'r': avg_color[0],
            'g': avg_color[1],
            'b': avg_color[2],
            'group': group_name,
            'relative_file_path': image_path
        }

        return Result.Success(cell_data)

    def save_image(self, image_data) -> None:
        db_logger.info(f'SAVED {image_data["relative_file_path"]}')

    def get_all_groups(self) -> list[str]:
        base_path = self.settings.image_groups_relative_path
        return [
            group_name
            for group_name
            in os.listdir(base_path)
            if os.path.isdir(os.path.join(base_path, group_name))
        ]

    def image_exists(self, image_path: str, group_name: str) -> bool:
        # TODO: --/--
        return False

    def delete_image_file(self, relative_path: str) -> None:
        try:
            os.remove(relative_path)
            db_logger.info(f'Image deleted at {relative_path}')
        except Exception as e:
            db_logger.error(f"Error deleting file: {e}")
