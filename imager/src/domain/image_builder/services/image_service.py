import math

from src.shared_kernel.resource_limits import configure_decoder_limits

configure_decoder_limits()

import cv2
import numpy as np
from scipy.spatial import KDTree
from src.shared_kernel.result import Result

from ..errors.image_service_errors import ImageServiceErrorMessages


class ImageService:
    @staticmethod
    def normalize_image(image: np.ndarray) -> Result:
        """Normalize decoded OpenCV pixels without changing their BGR order.

        Alpha is discarded, as in the existing renderer. 16-bit unsigned
        channels use their full range rather than being truncated to uint8.
        """
        try:
            if not isinstance(image, np.ndarray) or image.size == 0:
                raise ValueError('Image must be a non-empty pixel array')
            if image.ndim not in (2, 3):
                raise ValueError('Image must have two or three dimensions')
            if image.ndim == 3 and image.shape[2] not in (1, 3, 4):
                raise ValueError('Image must have one, three or four channels')
            if image.dtype == np.uint16:
                image = cv2.convertScaleAbs(image, alpha=255.0 / 65535.0)
            elif image.dtype != np.uint8:
                raise ValueError('Image pixel type must be uint8 or uint16')

            if image.ndim == 2 or image.shape[2] == 1:
                image = cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
            elif image.shape[2] == 4:
                image = cv2.cvtColor(image, cv2.COLOR_BGRA2BGR)
            return Result.Success(np.ascontiguousarray(image))
        except Exception as e:
            return ImageServiceErrorMessages.failed_to_normalize_image(str(e))

    @staticmethod
    def validate_image(
        image: np.ndarray,
        min_size: tuple[int, int] = (20, 20),
        max_size: tuple[int, int] = (4000, 4000),
        aspect_ratio_limits: tuple[float, float] | None = None,
    ) -> Result:
        """Check decoded dimensions; source and library policies stay separate."""
        try:
            if not isinstance(image, np.ndarray) or image.size == 0:
                raise ValueError('Image must be a non-empty pixel array')
            if image.ndim not in (2, 3):
                raise ValueError('Image must have two or three dimensions')
            height, width = image.shape[:2]
            if not (min_size[0] <= width <= max_size[0]
                    and min_size[1] <= height <= max_size[1]):
                raise ValueError(
                    f'Image dimensions {width}x{height} must be between '
                    f'{min_size[0]}x{min_size[1]} and '
                    f'{max_size[0]}x{max_size[1]}'
                )
            if aspect_ratio_limits is not None:
                ratio = width / height
                if not aspect_ratio_limits[0] <= ratio <= aspect_ratio_limits[1]:
                    raise ValueError(
                        f'Image aspect ratio {ratio:.4f} must be between '
                        f'{aspect_ratio_limits[0]:.4f} and '
                        f'{aspect_ratio_limits[1]:.4f}'
                    )
            return Result.Success(image)
        except Exception as e:
            return ImageServiceErrorMessages.invalid_image(str(e))

    @staticmethod
    def average_color(image: np.ndarray) -> Result:
        try:
            if not isinstance(image, np.ndarray) or image.size == 0:
                raise ValueError('Image must be a non-empty pixel array')
            channels = 1 if image.ndim == 2 else image.shape[2]
            if channels not in (1, 3, 4):
                raise ValueError('Image must have one, three or four channels')
            # OpenCV reduces directly, without temporary float pixel arrays.
            return Result.Success(tuple(map(int, cv2.mean(image)[:channels])))
        except Exception as e:
            return ImageServiceErrorMessages.failed_to_calculate_average_color(str(e))

    @staticmethod
    def resize_image(image: np.ndarray, width: int, height: int) -> Result:
        try:
            if (type(width) is not int or type(height) is not int
                    or width <= 0 or height <= 0):
                raise ValueError('Image dimensions must be positive integers')
            source_height, source_width = image.shape[:2]
            interpolation = (
                cv2.INTER_AREA
                if width <= source_width and height <= source_height
                else cv2.INTER_LINEAR
            )
            resized_image = cv2.resize(
                image, (width, height), interpolation=interpolation
            )
            return Result.Success(resized_image)
        except Exception as e:
            return ImageServiceErrorMessages.failed_to_resize_image(str(e))

    @staticmethod
    def crop_square_image(image: np.ndarray) -> Result:
        try:
            if not isinstance(image, np.ndarray) or image.size == 0:
                raise ValueError('Image must be a non-empty pixel array')
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
            if (type(width) is not int or type(height) is not int
                    or width <= 0 or height <= 0):
                raise ValueError('Image dimensions must be positive integers')
            template = np.zeros((height, width, 3), dtype=np.uint8)
            return Result.Success(template)
        except Exception as e:
            return ImageServiceErrorMessages.failed_to_create_template(str(e))

    @staticmethod
    def split_image(image: np.ndarray, num_horizontal_splits: int,
                    num_vertical_splits: int) -> Result:
        try:
            height, width = image.shape[:2]
            if (type(num_horizontal_splits) is not int
                    or type(num_vertical_splits) is not int
                    or not 1 <= num_horizontal_splits <= width
                    or not 1 <= num_vertical_splits <= height):
                raise ValueError('Split counts must fit within image dimensions')
            tiles = [
                tile
                for row in np.array_split(image, num_vertical_splits, axis=0)
                for tile in np.array_split(row, num_horizontal_splits, axis=1)
            ]
            return Result.Success(tiles)
        except Exception as e:
            return ImageServiceErrorMessages.failed_to_split_image(str(e))

    @staticmethod
    def overlay_image_alpha(background: np.ndarray, overlay: np.ndarray,
                            alpha: float = 0.5, *, inplace: bool = False,
                            chunk_rows: int = 128) -> Result:
        try:
            if (isinstance(alpha, bool) or not math.isfinite(alpha)
                    or not 0 <= alpha <= 1):
                raise ValueError('Overlay alpha must be between 0 and 1')
            if type(chunk_rows) is not int or chunk_rows <= 0:
                raise ValueError('Chunk row count must be a positive integer')
            if (background.ndim != 3 or background.shape[2] != 3
                    or overlay.ndim != 3 or overlay.shape[2] != 3
                    or background.dtype != overlay.dtype):
                raise ValueError('Overlay images must have matching three-channel types')
            result_image = background if inplace else background.copy()
            if alpha == 0:
                return Result.Success(result_image)

            if overlay.shape[:2] == background.shape[:2]:
                overlay_resized = overlay
            else:
                overlay_resized = cv2.resize(
                    overlay, (background.shape[1], background.shape[0])
                )
            if alpha == 1:
                np.copyto(result_image, overlay_resized)
                return Result.Success(result_image)

            # A caller-owned mosaic can be blended in place. This avoids a
            # third full-sized image allocation while retaining cv2 rounding.
            for start in range(0, background.shape[0], chunk_rows):
                end = start + chunk_rows
                cv2.addWeighted(
                    background[start:end], 1 - alpha,
                    overlay_resized[start:end], alpha, 0,
                    dst=result_image[start:end],
                )
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
            rgb_image = cv2.cvtColor(image, cv2.COLOR_BGRA2BGR)
            return Result.Success(rgb_image)
        except Exception as e:
            return ImageServiceErrorMessages.failed_to_convert_rgba_to_rgb(str(e))
