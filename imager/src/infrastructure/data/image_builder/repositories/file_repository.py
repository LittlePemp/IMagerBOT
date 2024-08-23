import os

import cv2
import numpy as np
from settings import settings
from src.domain.image_builder.services.image_service import ImageService
from src.shared_kernel.loggers import db_logger
from src.shared_kernel.result import Result


class FileRepository:
    def __init__(self):
        self.settings = settings
        self.image_service = ImageService()

    def read_image_file(self, relative_path: str) -> Result:
        image = cv2.imread(relative_path, cv2.IMREAD_UNCHANGED)
        if image is None:
            return Result.Error(f'Image not found at path: {relative_path}')
        return Result.Success(image)

    def save_image_file(self, image: np.ndarray, relative_path: str) -> Result:
        try:
            cv2.imwrite(relative_path, image)
            db_logger.info(f'Image saved at {relative_path}')
            return Result.Success('Image successfully saved')
        except Exception as e:
            db_logger.error(f'Cannot write image at {relative_path}: {str(e)}')
            return Result.Error(f'Cannot write image at {relative_path}: {str(e)}')

    def list_image_files(self, directory_path: str) -> Result:
        try:
            supported_extensions = ('.png', '.jpg', '.jpeg', '.bmp', '.tiff')
            files = [
                os.path.join(directory_path, f)
                for f in os.listdir(directory_path)
                if f.lower().endswith(supported_extensions)
            ]
            return Result.Success(files)
        except Exception as e:
            db_logger.error(f'Error listing image files in directory {directory_path}: {str(e)}')
            return Result.Error(f'Error listing image files in directory {directory_path}: {str(e)}')

    def load_image(self, image_path: str, group_name: str) -> Result:
        image_result = self.read_image_file(image_path)
        if not image_result.is_success:
            return image_result

        image = image_result.value
        avg_color_result = self.image_service.average_color(image)
        if not avg_color_result.is_success:
            return avg_color_result

        avg_color = avg_color_result.value

        cell_data = {
            'r': avg_color[0],
            'g': avg_color[1],
            'b': avg_color[2],
            'group': group_name,
            'relative_file_path': image_path
        }

        return Result.Success(cell_data)

    def get_all_groups(self) -> Result:
        try:
            base_path = self.settings.image_groups_relative_path
            groups = [
                group_name
                for group_name in os.listdir(base_path)
                if os.path.isdir(os.path.join(base_path, group_name))
            ]
            return Result.Success(groups)
        except Exception as e:
            db_logger.error(f'Error getting groups from {self.settings.image_groups_relative_path}: {str(e)}')
            return Result.Error(f'Error getting groups from {self.settings.image_groups_relative_path}: {str(e)}')

    def image_exists(self, image_path: str, group_name: str) -> Result:
        try:
            exists = os.path.isfile(image_path)
            return Result.Success(exists)
        except Exception as e:
            db_logger.error(f'Error checking if image exists at {image_path} for group {group_name}: {str(e)}')
            return Result.Error(f'Error checking if image exists at {image_path} for group {group_name}: {str(e)}')

    def delete_image_file(self, relative_path: str) -> Result:
        try:
            os.remove(relative_path)
            db_logger.info(f'Image deleted at {relative_path}')
            return Result.Success(f'Image deleted at {relative_path}')
        except Exception as e:
            db_logger.error(f'Error deleting file at {relative_path}: {str(e)}')
            return Result.Error(f'Error deleting file at {relative_path}: {str(e)}')
