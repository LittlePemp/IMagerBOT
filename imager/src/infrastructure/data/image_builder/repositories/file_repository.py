import os
import tempfile
from imager_config.loader import config

from src.shared_kernel.resource_limits import (
    MAX_SOURCE_FILE_BYTES, configure_decoder_limits, require_memory,
)

configure_decoder_limits()

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

    def resolve_image_path(self, image_path: str) -> str:
        normalized_path = os.path.normpath(image_path)
        if os.path.isfile(normalized_path):
            return normalized_path

        groups_marker = os.path.normpath(config.paths.image_groups)
        normalized_lower = normalized_path.lower()
        marker_lower = groups_marker.lower()

        marker_index = normalized_lower.rfind(marker_lower)
        if marker_index != -1:
            suffix = normalized_path[marker_index + len(groups_marker):].lstrip('\\/')
            candidate = os.path.normpath(os.path.join(self.settings.image_groups_relative_path, suffix))
            if os.path.isfile(candidate):
                return candidate

        path_parts = normalized_path.split(os.sep)
        if len(path_parts) >= 2:
            candidate = os.path.normpath(
                os.path.join(
                    self.settings.image_groups_relative_path,
                    path_parts[-2],
                    path_parts[-1],
                )
            )
            if os.path.isfile(candidate):
                return candidate

        return normalized_path

    def read_image_file(self, relative_path: str) -> Result:
        try:
            resolved_path = self.resolve_image_path(relative_path)
            if not os.path.isfile(resolved_path):
                raise ValueError(f'Image not found at path: {relative_path}')
            if os.path.getsize(resolved_path) > MAX_SOURCE_FILE_BYTES:
                raise ValueError(f'Encoded image exceeds {MAX_SOURCE_FILE_BYTES} bytes: {resolved_path}')
            # Worst supported uint16 BGRA source plus its uint8 normalization.
            require_memory(config.memory.decode_reserve_bytes)
            image = cv2.imread(resolved_path, cv2.IMREAD_UNCHANGED)
            if image is None:
                raise ValueError(f'Cannot decode image: {resolved_path}')
            validation = self.image_service.validate_image(
                image, min_size=self.settings.min_size, max_size=self.settings.max_size,
            )
            if not validation.is_success:
                raise ValueError(str(validation.error))
            return Result.Success(image)
        except Exception as exc:
            db_logger.error(f'Cannot read image {relative_path}: {exc}')
            return Result.Error(str(exc))

    def save_image_file(self, image: np.ndarray, relative_path: str, save_params=None) -> Result:
        temporary_path = None
        try:
            destination = os.path.realpath(relative_path)
            root = os.path.realpath(os.path.join(self.settings.file_path_prefix, self.settings.generated_images_path))
            if os.path.commonpath([root, destination]) != root:
                raise ValueError('Output image must be inside the generated images directory')
            os.makedirs(root, exist_ok=True)
            descriptor, temporary_path = tempfile.mkstemp(
                prefix=config.storage.local_result_temp_prefix,
                suffix=os.path.splitext(destination)[1], dir=os.path.dirname(destination)
            )
            os.close(descriptor)
            if save_params is None:
                written = cv2.imwrite(temporary_path, image)
            else:
                written = cv2.imwrite(temporary_path, image, save_params)
            if not written or os.path.getsize(temporary_path) == 0:
                raise OSError('Image encoder failed to write the result')
            os.replace(temporary_path, destination)
            temporary_path = None
            db_logger.info(f'Image saved at {relative_path}')
            return Result.Success('Image successfully saved')
        except Exception as e:
            db_logger.error(f'Cannot write image at {relative_path}: {str(e)}')
            return Result.Error(f'Cannot write image at {relative_path}: {str(e)}')
        finally:
            if temporary_path is not None:
                try:
                    os.remove(temporary_path)
                except OSError as exc:
                    db_logger.warning(f'Cannot remove temporary result {temporary_path}: {exc}')

    def list_image_files(self, directory_path: str) -> Result:
        try:
            supported_extensions = ('.png', '.jpg', '.jpeg', '.bmp', '.tiff', '.tif')
            files = [
                os.path.join(directory_path, f)
                for f in os.listdir(directory_path)
                if f.lower().endswith(supported_extensions) and os.path.isfile(os.path.join(directory_path, f))
            ]
            return Result.Success(sorted(files))
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
