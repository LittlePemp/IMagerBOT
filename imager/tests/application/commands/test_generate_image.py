import unittest
from unittest.mock import patch, MagicMock
from src.shared_kernel.result import Result
from src.application.image_builder.commands.generate_image import (
    GenerateImageCommand, GenerateImageCommandHandler
)
from src.infrastructure.data.image_builder.unit_of_work import MongoUnitOfWork


class TestGenerateImageCommandHandler(unittest.TestCase):

    def setUp(self):
        self.command = GenerateImageCommand(
            image_path='test_image_path.png',
            insertion_format='crop',
            alpha_channel=50,
            noise_level=10,
            cell_size=60,
            result_size=120,
            group_name='test_group'
        )
        self.handler = GenerateImageCommandHandler()

    @patch('src.application.image_builder.commands.generate_image.get_uow')
    def test_handle_success(self, mock_get_uow):
        mock_uow = MagicMock(spec=MongoUnitOfWork)
        mock_uow.file_repository = MagicMock()
        mock_uow.cell_repository = MagicMock()
        mock_uow.file_repository.read_image_file.return_value = Result.Success('image_data')
        mock_get_uow.return_value = mock_uow

        mock_builder = MagicMock()
        mock_builder.make_image.return_value = Result.Success('final_image_path')

        with patch('src.application.image_builder.commands.generate_image.ImagerBuilder', return_value=mock_builder):
            result = self.handler.handle(self.command)
            self.assertTrue(result.is_success)
            self.assertEqual(result.value, 'final_image_path')

    @patch('src.application.image_builder.commands.generate_image.get_uow')
    def test_handle_exception(self, mock_get_uow):
        mock_get_uow.side_effect = Exception('Test exception')

        result = self.handler.handle(self.command)

        self.assertFalse(result.is_success)
        self.assertIn('An error occurred', result.error)

    @patch('src.application.image_builder.commands.generate_image.get_uow')
    def test_handle_invalid_insertion_format(self, mock_get_uow):
        invalid_command = GenerateImageCommand(
            image_path='test_image_path.png',
            insertion_format='invalid_format',
            alpha_channel=50,
            noise_level=10,
            cell_size=60,
            result_size=120,
            group_name='test_group'
        )
        result = self.handler.handle(invalid_command)
        self.assertFalse(result.is_success)
        self.assertEqual(result.error, 'Invalid insertion format')

    @patch('src.application.image_builder.commands.generate_image.get_uow')
    def test_handle_invalid_alpha_channel(self, mock_get_uow):
        invalid_command = GenerateImageCommand(
            image_path='test_image_path.png',
            insertion_format='crop',
            alpha_channel=150,  # Invalid alpha channel
            noise_level=10,
            cell_size=60,
            result_size=120,
            group_name='test_group'
        )
        result = self.handler.handle(invalid_command)
        self.assertFalse(result.is_success)
        self.assertEqual(result.error, 'Alpha channel must be between 0 and 100')

    @patch('src.application.image_builder.commands.generate_image.get_uow')
    def test_handle_invalid_noise_level(self, mock_get_uow):
        invalid_command = GenerateImageCommand(
            image_path='test_image_path.png',
            insertion_format='crop',
            alpha_channel=50,
            noise_level=-10,  # Invalid noise level
            cell_size=60,
            result_size=120,
            group_name='test_group'
        )
        result = self.handler.handle(invalid_command)
        self.assertFalse(result.is_success)
        self.assertEqual(result.error, 'Noise level must be between 0 and 100')

    @patch('src.application.image_builder.commands.generate_image.get_uow')
    def test_handle_invalid_cell_size(self, mock_get_uow):
        invalid_command = GenerateImageCommand(
            image_path='test_image_path.png',
            insertion_format='crop',
            alpha_channel=50,
            noise_level=10,
            cell_size=-1,  # Invalid cell size
            result_size=120,
            group_name='test_group'
        )
        result = self.handler.handle(invalid_command)
        self.assertFalse(result.is_success)
        self.assertEqual(result.error, 'Cell size must be greater than 0')

    @patch('src.application.image_builder.commands.generate_image.get_uow')
    def test_handle_invalid_result_size(self, mock_get_uow):
        invalid_command = GenerateImageCommand(
            image_path='test_image_path.png',
            insertion_format='crop',
            alpha_channel=50,
            noise_level=10,
            cell_size=60,
            result_size=-1,  # Invalid result size
            group_name='test_group'
        )
        result = self.handler.handle(invalid_command)
        self.assertFalse(result.is_success)
        self.assertEqual(result.error, 'Result size must be greater than 0')

    @patch('src.application.image_builder.commands.generate_image.get_uow')
    def test_handle_invalid_group_name(self, mock_get_uow):
        invalid_command = GenerateImageCommand(
            image_path='test_image_path.png',
            insertion_format='crop',
            alpha_channel=50,
            noise_level=10,
            cell_size=60,
            result_size=120,
            group_name=''  # Invalid group name
        )
        result = self.handler.handle(invalid_command)
        self.assertFalse(result.is_success)
        self.assertEqual(result.error, 'Image group cannot be empty')
