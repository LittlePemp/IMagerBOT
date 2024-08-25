import cv2
import numpy as np
from scipy.spatial import KDTree
from src.shared_kernel.result import Result

from ..errors.image_service_errors import ImageServiceErrorMessages


class ImageService:
    @staticmethod
    def average_color(image: np.ndarray) -> Result:
        try:
            avg_color_per_row = np.average(image, axis=0)
            avg_color = np.average(avg_color_per_row, axis=0)
            return Result.Success(tuple(map(int, avg_color)))
        except Exception as e:
            return ImageServiceErrorMessages.failed_to_calculate_average_color(str(e))

    @staticmethod
    def resize_image(image: np.ndarray, width: int, height: int) -> Result:
        try:
            resized_image = cv2.resize(image, (width, height))
            return Result.Success(resized_image)
        except Exception as e:
            return ImageServiceErrorMessages.failed_to_resize_image(str(e))

    @staticmethod
    def crop_square_image(image: np.ndarray) -> Result:
        try:
            h, w = image.shape[:2]
            side_length = min(h, w)
            top = max(0, h // 2 - side_length // 2)
            left = max(0, w // 2 - side_length // 2)
            bottom = top + side_length
            right = left + side_length
            return Result.Success(image[top:bottom, left:right])
        except Exception as e:
            return ImageServiceErrorMessages.failed_to_crop_image(str(e))

    @staticmethod
    def create_template(width: int, height: int) -> Result:
        try:
            template = np.zeros((height, width, 3), dtype=np.uint8)
            return Result.Success(template)
        except Exception as e:
            return ImageServiceErrorMessages.failed_to_create_template(str(e))

    @staticmethod
    def split_image(image: np.ndarray, num_horizontal_splits: int,
                    num_vertical_splits: int) -> Result:
        try:
            height, width = image.shape[:2]
            M, N = (width // num_horizontal_splits,
                    height // num_vertical_splits)
            tiles = [image[x:x + N, y:y + M] for x in range(0, height, N)
                     for y in range(0, width, M)]
            return Result.Success(tiles)
        except Exception as e:
            return ImageServiceErrorMessages.failed_to_split_image(str(e))

    @staticmethod
    def overlay_image_alpha(background: np.ndarray, overlay: np.ndarray,
                            alpha: float = 0.5) -> Result:
        try:
            overlay_resized = cv2.resize(overlay,
                                         (background.shape[1],
                                          background.shape[0]))
            result_image = cv2.addWeighted(background, 1 - alpha,
                                           overlay_resized, alpha, 0)
            return Result.Success(result_image)
        except Exception as e:
            return ImageServiceErrorMessages.failed_to_overlay_image(str(e))

    @staticmethod
    def find_closest(tree: KDTree, target: tuple[int, int, int]) -> Result:
        try:
            distance, index = tree.query(target)
            return Result.Success((distance, index))
        except Exception as e:
            return ImageServiceErrorMessages.failed_to_find_closest_point(str(e))

    @staticmethod
    def distance_squared(point1: tuple[int, int, int],
                         point2: tuple[int, int, int]) -> Result:
        try:
            distance_sq = sum((p1 - p2) ** 2 for p1, p2 in zip(point1, point2))
            return Result.Success(distance_sq)
        except Exception as e:
            return ImageServiceErrorMessages.failed_to_calculate_distance_squared(str(e))

    @staticmethod
    def convert_rgba_to_rgb(image: np.ndarray) -> Result:
        try:
            rgb_image = cv2.cvtColor(image, cv2.COLOR_RGBA2RGB)
            return Result.Success(rgb_image)
        except Exception as e:
            return ImageServiceErrorMessages.failed_to_convert_rgba_to_rgb(str(e))
