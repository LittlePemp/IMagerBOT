import os
import numpy as np
from random import randint
from uuid import uuid4
from src.shared_kernel.result import Result
from src.infrastructure.data.image_builder.repositories.file_repository import FileRepository
from src.domain.image_builder.services.kdtree_service import KDTreeService
from src.shared_kernel.loggers import application_logger
from settings import settings
from src.domain.image_builder.services.image_service import ImageService
from src.infrastructure.data.image_builder.repositories.cell_repository import CellRepository


class ImagerBuilder:
    def __init__(
        self,
        file_repository: FileRepository,
        cell_repository: CellRepository,
        insertion_format: str,
        result_size: int,
        cell_size: int,
        alpha: float,
        noise_degree: int
    ):
        self.file_repository = file_repository
        self.cell_repository = cell_repository
        self.insertion_format = insertion_format
        self.result_size = result_size
        self.cell_size = cell_size
        self.alpha = alpha
        self.noise_degree = noise_degree
        self.kdtree_service = KDTreeService()
        self.kdtree_service.trees = self.kdtree_service.build_trees(cell_repository.data)

    def make_image(self, image_path: str, group: str) -> Result:
        try:
            image_result = self.file_repository.read_image_file(image_path)
            if not image_result.is_success:
                return image_result
            image = image_result.value

            if image.shape[2] == 4:
                image = ImageService.convert_rgba_to_rgb(image)

            small_width, small_height = self.calculate_small_image_dimensions(image)
            small_image = ImageService.resize_image(image, small_width, small_height)
            result_image = self.create_result_image(small_image, small_width, small_height, group)
            final_image = ImageService.overlay_image_alpha(result_image, image, self.alpha)

            final_image_path = self.save_image(final_image)
            return Result.Success(final_image_path)
        except Exception as e:
            application_logger.error(f'Error in make_image: {e}')
            return Result.Error(str(e))

    def calculate_small_image_dimensions(self, image: np.ndarray) -> tuple[int, int]:
        h, w = image.shape[:2]
        if h > w:
            dimensions = int(self.result_size * w / h), self.result_size
        else:
            dimensions = self.result_size, int(self.result_size * h / w)
        return dimensions

    def create_result_image(self, small_image: np.ndarray, small_width: int,
                            small_height: int, group: str) -> np.ndarray:
        tiles = ImageService.split_image(small_image, 1, 1)  # TODO:if need sharding
        result_image = ImageService.create_template(
            small_width * self.cell_size, small_height * self.cell_size)
        for i, tile in enumerate(tiles):
            tile_x = (i // 1) * (small_height // 1) * self.cell_size
            tile_y = (i % 1) * (small_width // 1) * self.cell_size
            result_tile = self.process_tile(tile, group)
            result_image[tile_x:tile_x + result_tile.shape[0], tile_y:tile_y + result_tile.shape[1]] = result_tile
        return result_image

    def process_tile(self, tile: np.ndarray, group: str) -> np.ndarray:
        tile_height, tile_width, num_channels = tile.shape
        result_tile = np.zeros((tile_height * self.cell_size, tile_width * self.cell_size, 3), dtype=np.uint8)
        for y in range(tile_height):
            for x in range(tile_width):
                pixel_rgb = tile[y, x]

                if num_channels == 4:
                    pixel_rgb = pixel_rgb[:3]

                noised_rgb = tuple(
                    max(0, min(255, int(color_value) + randint(-self.noise_degree, self.noise_degree)))
                    for color_value in tuple(pixel_rgb)
                )
                closest_cell = self.cell_repository.find_closest_cell(noised_rgb, group)
                if closest_cell is None:
                    application_logger.error(f'No closest cell found for pixel {noised_rgb} in group {group}')
                    continue
                cell_image = closest_cell.image
                if cell_image is None:
                    application_logger.error(f'Cell image is None for pixel {noised_rgb} in group {group}')
                    continue

                if self.insertion_format == 'crop':
                    cell_image = ImageService.crop_square_image(cell_image)
                cell_image = ImageService.resize_image(cell_image, self.cell_size, self.cell_size)

                y_big = y * self.cell_size
                x_big = x * self.cell_size
                result_tile[y_big: y_big + self.cell_size, x_big: x_big + self.cell_size] = cell_image
        return result_tile

    def save_image(self, final_image: np.ndarray) -> str:
        final_image_path = os.path.join(settings.generated_images_path, f'IMager_{uuid4()}.png')
        self.file_repository.save_image_file(final_image, final_image_path)
        return final_image_path
