import unittest
from unittest.mock import patch
import numpy as np
import os
import cv2
from src.domain.image_builder.entities.cell_object import CellObject
from src.domain.image_builder.aggregates.cell import Cell
from src.domain.image_builder.value_objects.cell_rgb import CellRgb


class TestCellObject(unittest.TestCase):

    def setUp(self):
        rgb_result = CellRgb.create(100, 150, 200)
        self.assertTrue(rgb_result.is_success)
        self.rgb = rgb_result.value
        self.group = "test_group"
        self.valid_path = "valid_image.png"
        self.invalid_path = "invalid_path/to/image.png"
        self.cell_result = Cell.create(self.rgb, self.group, self.valid_path)
        self.assertTrue(self.cell_result.is_success)
        self.cell = self.cell_result.value

        if not os.path.exists(self.valid_path):
            cv2.imwrite(self.valid_path, np.zeros((10, 10, 3)))

    def tearDown(self):
        if os.path.exists(self.valid_path):
            os.remove(self.valid_path)

    @patch('cv2.imread', return_value=np.zeros((10, 10, 3)))
    def test_create_valid(self, mock_imread):
        result = CellObject.create(self.cell)
        self.assertTrue(result.is_success)
        self.assertIsInstance(result.value, CellObject)
        self.assertIs(result.value.cell, self.cell)

    @patch('cv2.imread', return_value=None)
    def test_create_invalid_image_open(self, mock_imread):
        invalid_cell = Cell(self.rgb, self.group, self.invalid_path)
        result = CellObject.create(invalid_cell)
        self.assertFalse(result.is_success)
        self.assertIn("Error opening image", result.error)

    @patch('cv2.imread', return_value=np.zeros((10, 10, 3)))
    def test_create_from_image_valid(self, mock_imread):
        result = CellObject.create_from_image(self.rgb, self.group, self.valid_path)
        self.assertTrue(result.is_success)
        self.assertIsInstance(result.value, CellObject)
        self.assertEqual(result.value.cell.rgb, self.rgb)
        self.assertEqual(result.value.cell.group, self.group)
        self.assertEqual(result.value.cell.relative_file_path, self.valid_path)

    @patch('cv2.imread', return_value=None)
    def test_create_from_image_invalid_image_open(self, mock_imread):
        result = CellObject.create_from_image(self.rgb, self.group, self.invalid_path)
        self.assertFalse(result.is_success)
        self.assertIn("Error opening image", result.error)

    def test_str(self):
        cell_object = CellObject(self.cell, np.zeros((10, 10, 3)))
        self.assertEqual(str(cell_object), str(self.cell))
