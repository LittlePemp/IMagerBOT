import unittest
from src.domain.image_builder.aggregates.cell import Cell
from src.domain.image_builder.value_objects.cell_rgb import CellRgb


class TestCell(unittest.TestCase):

    def setUp(self):
        self.rgb = CellRgb.create(100, 150, 200).value
        self.group = 'test_group'
        self.relative_file_path = 'path/to/image.png'
        self.updated_rgb = CellRgb.create(50, 75, 100).value
        self.updated_group = 'updated_group'
        self.updated_relative_file_path = 'path/to/updated_image.png'

    def test_create_cell(self):
        result = Cell.create(self.rgb, self.group, self.relative_file_path)
        self.assertTrue(result.is_success)
        cell = result.value
        self.assertIsInstance(cell, Cell)
        self.assertEqual(cell.rgb, self.rgb)
        self.assertEqual(cell.group, self.group)
        self.assertEqual(cell.relative_file_path, self.relative_file_path)
