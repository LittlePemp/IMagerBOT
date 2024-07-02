import cv2
import numpy as np
from scipy.spatial import KDTree


class ImageService:
    @staticmethod
    def average_color(image: np.ndarray) -> tuple[int, int, int]:
        avg_color_per_row = np.average(image, axis=0)
        avg_color = np.average(avg_color_per_row, axis=0)
        return tuple(map(int, avg_color))

    @staticmethod
    def resize_image(image: np.ndarray, width: int, height: int) -> np.ndarray:
        return cv2.resize(image, (width, height))

    @staticmethod
    def create_template(width: int, height: int) -> np.ndarray:
        return np.zeros((height, width, 3), dtype=np.uint8)

    @staticmethod
    def split_image(image: np.ndarray, num_horizontal_splits: int, num_vertical_splits: int) -> list[np.ndarray]:
        height, width = image.shape[:2]
        M, N = width // num_horizontal_splits, height // num_vertical_splits
        tiles = [image[x:x + N, y:y + M] for x in range(0, height, N) for y in range(0, width, M)]
        return tiles

    @staticmethod
    def overlay_image_alpha(background: np.ndarray, overlay: np.ndarray, alpha: float = 0.5) -> np.ndarray:
        overlay_resized = cv2.resize(overlay, (background.shape[1], background.shape[0]))
        return cv2.addWeighted(background, 1 - alpha, overlay_resized, alpha, 0)

    @staticmethod
    def find_closest(tree: KDTree, target: tuple[int, int, int]) -> tuple:
        distance, index = tree.query(target)
        return distance, index

    @staticmethod
    def distance_squared(point1: tuple[int, int, int], point2: tuple[int, int, int]) -> int:
        return sum((p1 - p2) ** 2 for p1, p2 in zip(point1, point2))

    @staticmethod
    def convert_rgba_to_rgb(image: np.ndarray) -> np.ndarray:
        return cv2.cvtColor(image, cv2.COLOR_RGBA2RGB)
