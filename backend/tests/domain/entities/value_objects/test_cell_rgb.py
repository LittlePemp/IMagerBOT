import unittest
from imager.domain.image_builder.value_objects.cell_rgb import CellRgb


class TestCellRgb(unittest.TestCase):

    def setUp(self):
        self.valid_r = 100
        self.valid_g = 150
        self.valid_b = 200
        self.invalid_r = 300
        self.invalid_g = -10
        self.invalid_b = "blue"

    def test_create_valid_rgb(self):
        result = CellRgb.create(self.valid_r, self.valid_g, self.valid_b)
        self.assertTrue(result.is_success)
        cell_rgb = result.value
        self.assertIsInstance(cell_rgb, CellRgb)
        self.assertEqual(int(cell_rgb.r), self.valid_r)
        self.assertEqual(int(cell_rgb.g), self.valid_g)
        self.assertEqual(int(cell_rgb.b), self.valid_b)

    def test_create_invalid_rgb(self):
        result = CellRgb.create(self.invalid_r, self.valid_g, self.valid_b)
        self.assertFalse(result.is_success)
        self.assertEqual(result.error, 'RGB must be in [0..255]')

        result = CellRgb.create(self.valid_r, self.invalid_g, self.valid_b)
        self.assertFalse(result.is_success)
        self.assertEqual(result.error, 'RGB must be in [0..255]')

        result = CellRgb.create(self.valid_r, self.valid_g, self.invalid_b)
        self.assertFalse(result.is_success)
        self.assertEqual(result.error, 'RGB must be an integer')

    def test_str(self):
        result = CellRgb.create(self.valid_r, self.valid_g, self.valid_b)
        self.assertTrue(result.is_success)
        cell_rgb = result.value
        expected_str = f'Red: {self.valid_r}, Green: {self.valid_g}, Blue: {self.valid_b}.'
        self.assertEqual(str(cell_rgb), expected_str)
