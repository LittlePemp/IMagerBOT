import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import cv2
import numpy as np
from src.application.image_builder.services.imager_builder import ImagerBuilder
from src.application.image_builder.commands.generate_image import (
    GenerateImageCommand, GenerateImageCommandHandler,
)
from src.infrastructure.data.image_builder.repositories.file_repository import FileRepository
from src.shared_kernel.result import Result


class Catalog:
    def __init__(self):
        self.image = np.full((20, 30, 3), (40, 80, 160), dtype=np.uint8)
        self.metadata = SimpleNamespace(cell=SimpleNamespace(relative_file_path='cell.png'))
        self.search_count = 0
        self.read_count = 0

    def find_closest_metadata(self, colors, group):
        self.search_count += 1
        if group != 'test':
            return Result.Error('Image group is missing or empty')
        return Result.Success([self.metadata] * len(colors))

    def get_cell_image(self, metadata):
        self.read_count += 1
        return Result.Success(self.image)


class TestImagerBuilder(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = temporary.name
        self.settings = SimpleNamespace(
            min_size=(20, 20), max_size=(4000, 4000),
            aspect_ratio_limits=(9 / 16, 16 / 9), file_path_prefix='',
            generated_images_path=self.root, generated_image_extension='png',
            generated_image_jpeg_quality=90, telegram_max_document_size_bytes=47185920,
            generation_estimated_jpeg_bytes_per_pixel=1.0,
        )
        self.settings_patch = patch(
            'src.application.image_builder.services.imager_builder.settings', self.settings
        )
        self.settings_patch.start()
        self.addCleanup(self.settings_patch.stop)
        self.files = FileRepository()
        self.files.settings = self.settings
        self.files.read_image_file = Mock(
            return_value=Result.Success(np.full((24, 32, 3), 100, dtype=np.uint8))
        )
        self.catalog = Catalog()
        self.builder = ImagerBuilder(self.files, self.catalog, 'crop', 4, 3, 0, 0, seed=1)

    def test_render_dimensions_pixels_and_batch_cache(self):
        result = self.builder.make_image('source.png', 'test')
        self.assertTrue(result, result.error)
        pixels = cv2.imread(os.path.join(self.root, result.value))
        self.assertEqual(pixels.shape, (9, 12, 3))
        np.testing.assert_array_equal(pixels, np.full((9, 12, 3), (40, 80, 160), dtype=np.uint8))
        self.assertEqual(self.catalog.search_count, 1)
        self.assertEqual(self.catalog.read_count, 1)

    def test_one_cell_never_creates_zero_side(self):
        self.builder.result_size = 1
        result = self.builder.make_image('source.png', 'test')
        self.assertTrue(result, result.error)
        self.assertEqual(cv2.imread(os.path.join(self.root, result.value)).shape, (3, 3, 3))

    def test_grayscale_and_uint16_sources_are_normalized(self):
        for image in (
            np.full((24, 32), 50, dtype=np.uint8),
            np.full((24, 32, 3), 32768, dtype=np.uint16),
        ):
            self.files.read_image_file.return_value = Result.Success(image)
            self.builder.alpha = 1
            result = self.builder.make_image('source.png', 'test')
            self.assertTrue(result, result.error)
            pixels = cv2.imread(os.path.join(self.root, result.value))
            expected = 50 if image.dtype == np.uint8 else 128
            self.assertTrue(np.all(pixels == expected))

    def test_missing_group_is_logged_failure(self):
        with patch('src.application.image_builder.services.imager_builder.application_logger') as logger:
            result = self.builder.make_image('source.png', 'missing')
        self.assertFalse(result)
        self.assertIn('missing', result.error)
        self.assertTrue(logger.error.called)

    def test_missing_cell_is_failure(self):
        self.catalog.get_cell_image = Mock(return_value=Result.Success(None))
        result = self.builder.make_image('source.png', 'test')
        self.assertFalse(result)
        self.assertIn('Cell image is None', result.error)

    def test_no_error_none_success(self):
        self.assertFalse(Result.Error(None))
        self.assertIsNotNone(Result.Error(None).error)

    def test_names_are_unique_and_keep_user_stem_inside_root(self):
        first = self.builder.make_image('source.png', 'test', 'alice_20260415')
        second = self.builder.make_image('source.png', 'test', 'alice_20260415')
        self.assertTrue(first, first.error)
        self.assertTrue(second, second.error)
        self.assertNotEqual(first.value, second.value)
        self.assertTrue(first.value.startswith('alice_20260415_'))
        escaped = self.builder.make_image('source.png', 'test', '../../outside')
        self.assertTrue(escaped, escaped.error)
        self.assertEqual(os.path.basename(escaped.value), escaped.value)
        self.assertEqual(len(os.listdir(self.root)), 3)

    def test_memory_guard_refuses_before_catalog_or_output_allocation(self):
        with patch('src.shared_kernel.resource_limits.current_rss_bytes', return_value=990_000_000):
            result = self.builder.make_image('source.png', 'test')
        self.assertFalse(result)
        self.assertIn('memory budget', result.error)
        self.assertEqual(self.catalog.search_count, 0)
        self.assertEqual(os.listdir(self.root), [])

    def test_actual_output_size_limit_is_enforced(self):
        self.settings.telegram_max_document_size_bytes = 1
        result = self.builder.make_image('source.png', 'test')
        self.assertFalse(result)
        self.assertEqual(os.listdir(self.root), [])

    def test_invalid_command_does_not_initialize_infrastructure(self):
        factory = Mock(side_effect=AssertionError('Infrastructure must not be called'))
        handler = GenerateImageCommandHandler(uow_factory=factory)
        command = GenerateImageCommand(
            'source.png', 'crop', 30, True, 40, 120, 'test'
        )
        result = handler.handle(command)
        self.assertFalse(result)
        factory.assert_not_called()
