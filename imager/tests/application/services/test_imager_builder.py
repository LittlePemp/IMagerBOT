import unittest
from unittest.mock import Mock, patch
import numpy as np
from src.application.image_builder.services.imager_builder import ImagerBuilder
from src.shared_kernel.result import Result
from src.infrastructure.data.image_builder.repositories.file_repository import FileRepository
from src.infrastructure.data.image_builder.repositories.cell_repository import CellRepository


class TestImagerBuilder(unittest.TestCase):

    def setUp(self):
        self.file_repository = Mock(spec=FileRepository)
        self.cell_repository = Mock(spec=CellRepository)
        self.cell_repository.find_closest_cell = Mock(return_value=Mock(image=np.random.randint(0, 255, (10, 10, 3),
                                                                                                dtype=np.uint8)))

        cell_mock = Mock()
        cell_mock.cell.rgb.r.value = 100
        cell_mock.cell.rgb.g.value = 150
        cell_mock.cell.rgb.b.value = 200
        self.cell_repository.data = {
            'test_group': {
                'cell1': cell_mock
            }
        }

        self.builder = ImagerBuilder(
            file_repository=self.file_repository,
            cell_repository=self.cell_repository,
            insertion_format='crop',
            result_size=120,
            cell_size=60,
            alpha=0.3,
            noise_degree=10
        )

        self.image_path = 'test_image.png'
        self.group = 'test_group'
        self.image_rgb = np.random.randint(0, 255, (100, 100, 3), dtype=np.uint8)
        self.image_rgba = np.random.randint(0, 255, (100, 100, 4), dtype=np.uint8)

    def test_make_image_success(self):
        self.file_repository.read_image_file.return_value = Result.Success(self.image_rgba)

        result = self.builder.make_image(self.image_path, self.group)

        self.assertTrue(result.is_success)
        self.file_repository.read_image_file.assert_called_once_with(self.image_path)

    def test_make_image_failure_read_image(self):
        self.file_repository.read_image_file.return_value = Result.Error('Failed to read image')

        result = self.builder.make_image(self.image_path, self.group)

        self.assertFalse(result.is_success)
        self.assertIn('Failed to read image', result.error)

    def test_make_image_failure_save_image(self):
        self.file_repository.read_image_file.return_value = Result.Success(self.image_rgba)
        self.file_repository.save_image_file.return_value = Result.Error('Failed to save image')

        result = self.builder.make_image(self.image_path, self.group)

        self.assertFalse(result.is_success)
        self.assertIn('Failed to save image', result.error)

    def test_calculate_small_image_dimensions(self):
        dimensions = self.builder.calculate_small_image_dimensions(self.image_rgb)
        self.assertEqual(dimensions, (120, 120))

    def test_create_result_image_failure(self):
        with patch('src.application.image_builder.services.imager_builder.ImageService.split_image', return_value=Result.Error('Split image error')):
            result = self.builder.create_result_image(self.image_rgb, 60, 60, self.group)

        self.assertFalse(result.is_success)
        self.assertIn('Split image error', result.error)

    def test_save_image_failure(self):
        self.file_repository.save_image_file.return_value = Result.Error('Failed to save image')
        final_image = np.random.randint(0, 255, (120, 120, 3), dtype=np.uint8)

        result = self.builder.save_image(final_image)

        self.assertFalse(result.is_success)
        self.assertIn('Failed to save image', result.error)
        self.file_repository.save_image_file.assert_called_once()
