import unittest
from unittest.mock import Mock, patch
import numpy as np
from imager.application.image_builder.services.imager_builder import ImagerBuilder
from imager.shared_kernel.result import Result
from imager.infrastructure.data.image_builder.repositories.file_repository import FileRepository
from imager.infrastructure.data.image_builder.repositories.cell_repository import CellRepository


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

    @patch('imager.application.image_builder.services.imager_builder.ImageService.convert_rgba_to_rgb')
    @patch('imager.application.image_builder.services.imager_builder.ImageService.resize_image')
    @patch('imager.application.image_builder.services.imager_builder.ImageService.overlay_image_alpha')
    def test_make_image_failure(self, mock_overlay, mock_resize, mock_convert):
        self.file_repository.read_image_file.return_value = Result.Error('Failed to read image')

        result = self.builder.make_image(self.image_path, self.group)
        self.assertFalse(result.is_success)
        self.assertEqual(result.error, 'Failed to read image')

    def test_calculate_small_image_dimensions(self):
        dimensions = self.builder.calculate_small_image_dimensions(self.image_rgb)
        self.assertEqual(dimensions, (120, 120))

    @patch('imager.application.image_builder.services.imager_builder.ImageService.split_image',
           return_value=[np.random.randint(0, 255, (60, 60, 3), dtype=np.uint8)])
    @patch('imager.application.image_builder.services.imager_builder.ImageService.create_template',
           return_value=np.zeros((3600, 3600, 3), dtype=np.uint8))
    def test_create_result_image(self, mock_create_template, mock_split_image):
        small_image = np.random.randint(0, 255, (60, 60, 3), dtype=np.uint8)
        result_image = self.builder.create_result_image(small_image, 60, 60, self.group)
        self.assertEqual(result_image.shape, (3600, 3600, 3))
        mock_create_template.assert_called_once()
        mock_split_image.assert_called_once()

    @patch('imager.application.image_builder.services.imager_builder.ImageService.crop_square_image')
    @patch('imager.application.image_builder.services.imager_builder.ImageService.resize_image')
    def test_process_tile_with_crop(self, mock_resize, mock_crop):
        mock_crop.return_value = np.random.randint(0, 255, (60, 60, 3), dtype=np.uint8)
        mock_resize.return_value = np.random.randint(0, 255, (60, 60, 3), dtype=np.uint8)
        tile = np.random.randint(0, 255, (1, 1, 3), dtype=np.uint8)
        result_tile = self.builder.process_tile(tile, self.group)
        self.assertEqual(result_tile.shape, (60, 60, 3))
        mock_crop.assert_called()
        mock_resize.assert_called()

    @patch('imager.application.image_builder.services.imager_builder.ImageService.resize_image')
    def test_process_tile_with_scale(self, mock_resize):
        self.builder.insertion_format = 'scale'
        mock_resize.return_value = np.random.randint(0, 255, (60, 60, 3), dtype=np.uint8)
        tile = np.random.randint(0, 255, (1, 1, 3), dtype=np.uint8)
        result_tile = self.builder.process_tile(tile, self.group)
        self.assertEqual(result_tile.shape, (60, 60, 3))
        mock_resize.assert_called()

    def test_save_image(self):
        final_image = np.random.randint(0, 255, (120, 120, 3), dtype=np.uint8)
        path = self.builder.save_image(final_image)
        self.assertTrue(path.endswith('.png'))
        self.file_repository.save_image_file.assert_called_once()
