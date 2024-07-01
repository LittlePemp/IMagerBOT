# backend/imager/application/image_builder/services/imager_builder.py

import os
import numpy as np
from random import randint
from uuid import uuid4

from imager.domain.image_builder.services.image_service import ImageService
from imager.infrastructure.data.image_builder.repositories.file_repository import FileRepository
from imager.shared_kernel.result import Result


class ImagerBuilder:
    def __init__(
        self,
        file_repository: FileRepository,
        image_service: ImageService,
        db_context,
        group='dota_icons',
        result_size=120,
        cell_size=60,
        alpha=0.3,
        noise_degree=10
    ):
        self.file_repository = file_repository
        self.image_service = image_service
        self.db_context = db_context
        self.group = group
        self.result_size = result_size
        self.cell_size = cell_size
        self.alpha = alpha
        self.noise_degree = noise_degree

    def make_image(self, image_path: str) -> Result:
        # Чтение изображения
        image = self.file_repository.read_image_file(image_path)

        # Обработка изображения
        small_width, small_height = self.calculate_small_image_dimensions(image)
        small_image = self.image_service.resize_image(image, small_width, small_height)
        result_image = self.create_result_image(small_image, small_width, small_height)
        final_image = self.image_service.overlay_image_alpha(result_image, image, self.alpha)

        # Сохранение изображения
        final_image_path = self.save_image(final_image)
        return Result.Success(final_image_path)

    def calculate_small_image_dimensions(self, image: np.ndarray) -> tuple[int, int]:
        h, w = image.shape[:2]
        if h > w:
            dimensions = int(self.result_size * w / h), self.result_size
        else:
            dimensions = self.result_size, int(self.result_size * h / w)
        return dimensions

    def create_result_image(self, small_image: np.ndarray, small_width: int, small_height: int) -> np.ndarray:
        tiles = self.image_service.split_image(small_image, 1, 1)
        result_image = self.image_service.create_template(small_width * self.cell_size, small_height * self.cell_size)
        for i, tile in enumerate(tiles):
            tile_x = (i // 1) * (small_height // 1) * self.cell_size
            tile_y = (i % 1) * (small_width // 1) * self.cell_size
            result_tile = self.process_tile(tile, tile_x, tile_y)
            result_image[tile_x:tile_x+result_tile.shape[0], tile_y:tile_y+result_tile.shape[1]] = result_tile
        return result_image

    def process_tile(self, tile: np.ndarray, tile_x: int, tile_y: int) -> np.ndarray:
        tile_height, tile_width, _ = tile.shape
        result_tile = np.zeros((tile_height * self.cell_size, tile_width * self.cell_size, 3), dtype=np.uint8)
        for y in range(tile_height):
            for x in range(tile_width):
                pixel_rgb = tile[y, x]
                noised_rgb = tuple(
                    color_value + randint(-self.noise_degree, self.noise_degree)
                    for color_value
                    in tuple(pixel_rgb)
                )
                closest_cell = self.find_closest_cell(noised_rgb, self.db_context.cell_object_repository.data)
                cell_image = closest_cell.image
                cell_image_resized = self.image_service.resize_image(cell_image, self.cell_size, self.cell_size)
                y_big = y * self.cell_size
                x_big = x * self.cell_size
                result_tile[y_big:y_big+self.cell_size, x_big:x_big+self.cell_size] = cell_image_resized
        return result_tile

    def save_image(self, final_image: np.ndarray) -> str:
        final_image_path = os.path.join('path/to/save', f'IMager_{uuid4()}.png')
        self.file_repository.save_image_file(final_image, final_image_path)
        return final_image_path
