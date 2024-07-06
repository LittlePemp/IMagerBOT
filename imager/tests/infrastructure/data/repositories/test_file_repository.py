import os
import unittest
from unittest.mock import patch

import cv2
import numpy as np
from src.infrastructure.data.image_builder.repositories.file_repository import \
    FileRepository


class TestFileRepository(unittest.TestCase):

    def setUp(self):
        self.file_repository = FileRepository()

    @patch('cv2.imread')
    def test_read_image_file_success(self, mock_imread):
        mock_image = np.zeros((10, 10, 3), dtype=np.uint8)
        mock_imread.return_value = mock_image
        relative_path = 'path/to/image.png'

        result = self.file_repository.read_image_file(relative_path)

        self.assertTrue(result.is_success)
        self.assertEqual(result.value.shape, mock_image.shape)
        mock_imread.assert_called_once_with(relative_path,
                                            cv2.IMREAD_UNCHANGED)

    @patch('cv2.imread')
    def test_read_image_file_not_found(self, mock_imread):
        mock_imread.return_value = None
        relative_path = 'path/to/image.png'

        result = self.file_repository.read_image_file(relative_path)

        self.assertFalse(result.is_success)
        self.assertEqual(result.error, f'Image not found at path: {relative_path}')
        mock_imread.assert_called_once_with(relative_path,
                                            cv2.IMREAD_UNCHANGED)

    @patch('cv2.imwrite')
    @patch('loguru.logger.info')
    def test_save_image_file_success(self, mock_logger_info, mock_imwrite):
        mock_image = np.zeros((10, 10, 3), dtype=np.uint8)
        relative_path = 'path/to/image.png'
        mock_imwrite.return_value = True

        result = self.file_repository.save_image_file(mock_image,
                                                      relative_path)

        self.assertTrue(result.is_success)
        mock_imwrite.assert_called_once_with(relative_path, mock_image)

    @patch('cv2.imwrite')
    @patch('loguru.logger.error')
    def test_save_image_file_error(self, mock_logger_error, mock_imwrite):
        mock_image = np.zeros((10, 10, 3), dtype=np.uint8)
        relative_path = 'path/to/image.png'
        mock_imwrite.side_effect = Exception('Cannot write image')

        result = self.file_repository.save_image_file(mock_image,
                                                      relative_path)

        self.assertFalse(result.is_success)
        self.assertEqual(str(result.error), 'Cannot write image')
        mock_imwrite.assert_called_once_with(relative_path, mock_image)

    @patch('os.listdir')
    def test_list_image_files(self, mock_listdir):
        mock_listdir.return_value = ['image1.png',
                                     'image2.jpg',
                                     'document.txt']
        directory_path = 'path/to/directory'

        result = self.file_repository.list_image_files(directory_path)

        expected_result = [
            os.path.join(directory_path, 'image1.png'),
            os.path.join(directory_path, 'image2.jpg')
        ]
        self.assertEqual(len(result), 2)
        self.assertIn(expected_result[0], result)
        self.assertIn(expected_result[1], result)
        self.assertNotIn(os.path.join(directory_path, 'document.txt'), result)
        mock_listdir.assert_called_once_with(directory_path)

    @patch('os.remove')
    @patch('loguru.logger.info')
    def test_delete_image_file_success(self, mock_logger_info, mock_remove):
        relative_path = 'path/to/image.png'

        self.file_repository.delete_image_file(relative_path)

        mock_remove.assert_called_once_with(relative_path)

    @patch('os.remove')
    @patch('loguru.logger.error')
    def test_delete_image_file_error(self, mock_logger_error, mock_remove):
        relative_path = 'path/to/image.png'
        mock_remove.side_effect = Exception('Error deleting file')

        self.file_repository.delete_image_file(relative_path)

        mock_remove.assert_called_once_with(relative_path)
