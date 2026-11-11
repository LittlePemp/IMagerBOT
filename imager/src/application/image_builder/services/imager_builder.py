import os
import re
from collections import OrderedDict
from datetime import datetime, timezone
from uuid import uuid4

from src.shared_kernel.resource_limits import configure_decoder_limits

configure_decoder_limits()

import cv2
import numpy as np
from settings import settings
from src.application.image_builder.errors.services_errors import ServicesErrorMessages
from src.domain.image_builder.repository_interface import ImageCatalog, ImageFiles
from src.domain.image_builder.services.image_service import ImageService
from src.shared_kernel.loggers import application_logger
from src.shared_kernel.resource_limits import (
    MAX_CELLS, MAX_OUTPUT_PIXELS, PREPARED_CACHE_LIMIT_BYTES,
    generation_slot, require_memory,
)
from src.shared_kernel.result import Result


class ImagerBuilder:
    def __init__(self, file_repository: ImageFiles, cell_repository: ImageCatalog,
                 insertion_format: str, result_size: int, cell_size: int,
                 alpha: float, noise_degree: int, seed: int | None = None):
        self.file_repository = file_repository
        self.cell_repository = cell_repository
        self.insertion_format = insertion_format
        self.result_size = result_size
        self.cell_size = cell_size
        self.alpha = alpha
        self.noise_degree = noise_degree
        self.rng = np.random.default_rng(seed)

    @staticmethod
    def _checked(result, stage):
        if not result.is_success:
            application_logger.error(f'{stage}: {result.error}')
        return result

    def make_image(self, image_path: str, group: str,
                   output_name: str | None = None) -> Result:
        with generation_slot():
            try:
                image_result = self._checked(
                    self.file_repository.read_image_file(image_path), 'Read source'
                )
                if not image_result:
                    return image_result
                normalized = self._checked(
                    ImageService.normalize_image(image_result.value), 'Normalize source'
                )
                if not normalized:
                    return normalized
                image = normalized.value
                image_result = normalized = None
                validation = self._checked(ImageService.validate_image(
                    image, min_size=settings.min_size, max_size=settings.max_size,
                    aspect_ratio_limits=settings.aspect_ratio_limits,
                ), 'Validate source')
                if not validation:
                    return validation

                small_width, small_height = self.calculate_small_image_dimensions(image)
                self._check_render_budget(small_width, small_height)
                resized = self._checked(
                    ImageService.resize_image(image, small_width, small_height),
                    'Resize source',
                )
                if not resized:
                    return resized
                assembled = self.create_result_image(
                    resized.value, small_width, small_height, group
                )
                if not assembled:
                    return self._checked(assembled, 'Assemble mosaic')
                mosaic = assembled.value
                require_memory(mosaic.nbytes if self.alpha else 0)
                blended = self._checked(ImageService.overlay_image_alpha(
                    mosaic, image, self.alpha, inplace=True,
                ), 'Blend source')
                if not blended:
                    return blended
                return self._checked(self.save_image(blended.value, output_name),
                                     'Save mosaic')
            except Exception as error:
                return ServicesErrorMessages.error_in_make_image(error)

    def _check_render_budget(self, width, height):
        cells = width * height
        pixels = cells * self.cell_size ** 2
        if cells > MAX_CELLS:
            raise ValueError(f'Mosaic cell count {cells} exceeds {MAX_CELLS}')
        if pixels > MAX_OUTPUT_PIXELS:
            raise ValueError(f'Output pixel count {pixels} exceeds {MAX_OUTPUT_PIXELS}')
        # Mosaic + resized original + bounded prepared tiles. The shared
        # guard additionally leaves room for native codec allocations.
        require_memory(6 * pixels + PREPARED_CACHE_LIMIT_BYTES)

    def calculate_small_image_dimensions(self, image: np.ndarray) -> tuple[int, int]:
        height, width = image.shape[:2]
        if height > width:
            return max(1, self.result_size * width // height), self.result_size
        return self.result_size, max(1, self.result_size * height // width)

    def create_result_image(self, small_image: np.ndarray, small_width: int,
                            small_height: int, group: str) -> Result:
        # Render into one destination; the old 1x1 split duplicated it.
        return self.process_tile(small_image, group)

    def process_tile(self, tile: np.ndarray, group: str) -> Result:
        try:
            height, width = tile.shape[:2]
            colors = tile[:, :, :3].reshape(-1, 3).astype(np.int16)
            if self.noise_degree:
                colors += self.rng.integers(
                    -self.noise_degree, self.noise_degree + 1,
                    size=colors.shape, dtype=np.int16,
                )
                np.clip(colors, 0, 255, out=colors)
            matched = self.cell_repository.find_closest_metadata(colors, group)
            if not matched:
                return self._checked(matched, 'Find mosaic cells')
            cells = matched.value
            if len(cells) != height * width:
                return Result.Error('Catalog returned an invalid number of mosaic cells')
            result = np.empty(
                (height * self.cell_size, width * self.cell_size, 3), dtype=np.uint8
            )
            prepared = OrderedDict()
            prepared_bytes = 0
            for index, cell in enumerate(cells):
                if cell is None:
                    return Result.Error(ServicesErrorMessages.no_closest_cell_found(
                        tuple(colors[index]), group,
                    ))
                key = (cell.cell.relative_file_path, self.cell_size, self.insertion_format)
                cell_image = prepared.get(key)
                if cell_image is not None:
                    prepared.move_to_end(key)
                else:
                    original = self.cell_repository.get_cell_image(cell)
                    if not original:
                        return self._checked(original, 'Read mosaic cell')
                    cell_image = original.value
                    if cell_image is None:
                        return Result.Error(ServicesErrorMessages.cell_image_is_none(
                            tuple(colors[index]), group,
                        ))
                    if self.insertion_format == 'crop':
                        cropped = ImageService.crop_square_image(cell_image)
                        if not cropped:
                            return self._checked(cropped, 'Crop mosaic cell')
                        cell_image = cropped.value
                    resized = ImageService.resize_image(
                        cell_image, self.cell_size, self.cell_size,
                    )
                    if not resized:
                        return self._checked(resized, 'Resize mosaic cell')
                    cell_image = resized.value
                    if cell_image.nbytes <= PREPARED_CACHE_LIMIT_BYTES:
                        while prepared and prepared_bytes + cell_image.nbytes > PREPARED_CACHE_LIMIT_BYTES:
                            _, removed = prepared.popitem(last=False)
                            prepared_bytes -= removed.nbytes
                        prepared[key] = cell_image
                        prepared_bytes += cell_image.nbytes
                y, x = divmod(index, width)
                result[y * self.cell_size:(y + 1) * self.cell_size,
                       x * self.cell_size:(x + 1) * self.cell_size] = cell_image
            return Result.Success(result)
        except Exception as error:
            return ServicesErrorMessages.error_in_make_image(error)

    def save_image(self, final_image: np.ndarray,
                   output_name: str | None = None) -> Result:
        extension = settings.generated_image_extension.lower().lstrip('.')
        if extension not in ('jpg', 'jpeg', 'png'):
            return Result.Error('Output format must be jpg, jpeg or png')
        # Preserve the Telegram username/date stem and add a server suffix.
        stem = re.sub(r'[^\w-]+', '_', output_name or 'imager').strip('_')[:96] or 'imager'
        timestamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        name = f'{stem}_{timestamp}_{uuid4().hex[:12]}.{extension}'
        root = os.path.abspath(os.path.join(settings.file_path_prefix,
                                            settings.generated_images_path))
        os.makedirs(root, exist_ok=True)
        path = os.path.join(root, name)
        params = None
        if extension in ('jpg', 'jpeg'):
            params = [int(cv2.IMWRITE_JPEG_QUALITY), settings.generated_image_jpeg_quality]
        saved = self.file_repository.save_image_file(final_image, path, save_params=params)
        if not saved:
            return saved
        if not os.path.isfile(path):
            return Result.Error('Output file is missing after saving')
        if os.path.getsize(path) > settings.telegram_max_document_size_bytes:
            os.remove(path)
            return Result.Error('Output file exceeds the configured delivery size limit')
        return Result.Success(name)
