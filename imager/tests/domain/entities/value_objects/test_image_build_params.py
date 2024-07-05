import unittest
from src.domain.image_builder.value_objects.image_build_params import (
    ImageInsertionFormat, AlphaChannel, NoiseLevel, CellSize, ResultSize, ImageGroup
)


class TestImageBuildParams(unittest.TestCase):

    def test_image_insertion_format_valid(self):
        result = ImageInsertionFormat.create(ImageInsertionFormat.CROP)
        self.assertTrue(result.is_success)
        self.assertEqual(result.value.value, ImageInsertionFormat.CROP)

        result = ImageInsertionFormat.create(ImageInsertionFormat.SCALE)
        self.assertTrue(result.is_success)
        self.assertEqual(result.value.value, ImageInsertionFormat.SCALE)

    def test_image_insertion_format_invalid(self):
        result = ImageInsertionFormat.create('invalid')
        self.assertFalse(result.is_success)
        self.assertEqual(result.error, 'Invalid insertion format')

    def test_alpha_channel_valid(self):
        result = AlphaChannel.create(50)
        self.assertTrue(result.is_success)
        self.assertEqual(result.value.value, 50)

    def test_alpha_channel_invalid(self):
        result = AlphaChannel.create(-1)
        self.assertFalse(result.is_success)
        self.assertEqual(result.error, 'Alpha channel must be between 0 and 100')

        result = AlphaChannel.create(101)
        self.assertFalse(result.is_success)
        self.assertEqual(result.error, 'Alpha channel must be between 0 and 100')

        result = AlphaChannel.create('invalid')
        self.assertFalse(result.is_success)
        self.assertEqual(result.error, 'Alpha channel must be an integer')

    def test_noise_level_valid(self):
        result = NoiseLevel.create(50)
        self.assertTrue(result.is_success)
        self.assertEqual(result.value.value, 50)

    def test_noise_level_invalid(self):
        result = NoiseLevel.create(-1)
        self.assertFalse(result.is_success)
        self.assertEqual(result.error, 'Noise level must be between 0 and 100')

        result = NoiseLevel.create(101)
        self.assertFalse(result.is_success)
        self.assertEqual(result.error, 'Noise level must be between 0 and 100')

        result = NoiseLevel.create('invalid')
        self.assertFalse(result.is_success)
        self.assertEqual(result.error, 'Noise level must be an integer')

    def test_cell_size_valid(self):
        result = CellSize.create(10)
        self.assertTrue(result.is_success)
        self.assertEqual(result.value.value, 10)

    def test_cell_size_invalid(self):
        result = CellSize.create(0)
        self.assertFalse(result.is_success)
        self.assertEqual(result.error, 'Cell size must be greater than 0')

        result = CellSize.create(-1)
        self.assertFalse(result.is_success)
        self.assertEqual(result.error, 'Cell size must be greater than 0')

        result = CellSize.create('invalid')
        self.assertFalse(result.is_success)
        self.assertEqual(result.error, 'Cell size must be an integer')

    def test_result_size_valid(self):
        result = ResultSize.create(10)
        self.assertTrue(result.is_success)
        self.assertEqual(result.value.value, 10)

    def test_result_size_invalid(self):
        result = ResultSize.create(0)
        self.assertFalse(result.is_success)
        self.assertEqual(result.error, 'Result size must be greater than 0')

        result = ResultSize.create(-1)
        self.assertFalse(result.is_success)
        self.assertEqual(result.error, 'Result size must be greater than 0')

        result = ResultSize.create('invalid')
        self.assertFalse(result.is_success)
        self.assertEqual(result.error, 'Result size must be an integer')

    def test_image_group_valid(self):
        result = ImageGroup.create('valid_group')
        self.assertTrue(result.is_success)
        self.assertEqual(result.value.value, 'valid_group')

    def test_image_group_invalid(self):
        result = ImageGroup.create('')
        self.assertFalse(result.is_success)
        self.assertEqual(result.error, 'Image group cannot be empty')

        result = ImageGroup.create(123)
        self.assertFalse(result.is_success)
        self.assertEqual(result.error, 'Image group must be a string')
