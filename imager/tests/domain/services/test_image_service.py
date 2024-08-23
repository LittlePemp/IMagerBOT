import unittest

import numpy as np
from src.domain.image_builder.services.image_service import ImageService


class TestImageService(unittest.TestCase):

    def setUp(self):
        self.image_rgb = np.random.randint(0, 255, (100, 100, 3), dtype=np.uint8)
        self.image_rgba = np.random.randint(0, 255, (100, 100, 4), dtype=np.uint8)

    def test_average_color(self):
        result = ImageService.average_color(self.image_rgb)
        expected_avg = tuple(map(int, np.average(self.image_rgb, axis=(0, 1))))
        self.assertTrue(result.is_success)
        self.assertEqual(result.value, expected_avg)

    def test_resize_image(self):
        result = ImageService.resize_image(self.image_rgb, 50, 50)
        self.assertTrue(result.is_success)
        self.assertEqual(result.value.shape, (50, 50, 3))

    def test_create_template(self):
        result = ImageService.create_template(100, 200)
        self.assertTrue(result.is_success)
        self.assertEqual(result.value.shape, (200, 100, 3))
        self.assertTrue(np.all(result.value == 0))

    def test_split_image(self):
        result = ImageService.split_image(self.image_rgb, 2, 2)
        self.assertTrue(result.is_success)
        self.assertEqual(len(result.value), 4)
        for split in result.value:
            self.assertEqual(split.shape, (50, 50, 3))

    def test_overlay_image_alpha(self):
        overlay_image = np.random.randint(0, 255, (100, 100, 3), dtype=np.uint8)
        result = ImageService.overlay_image_alpha(self.image_rgb, overlay_image, alpha=0.5)
        self.assertTrue(result.is_success)
        self.assertEqual(result.value.shape, self.image_rgb.shape)

    def test_distance_squared(self):
        point1 = (10, 10, 10)
        point2 = (13, 14, 15)
        result = ImageService.distance_squared(point1, point2)
        expected_dist = 50
        self.assertTrue(result.is_success)
        self.assertEqual(result.value, expected_dist)

    def test_convert_rgba_to_rgb(self):
        result = ImageService.convert_rgba_to_rgb(self.image_rgba)
        self.assertTrue(result.is_success)
        self.assertEqual(result.value.shape, (100, 100, 3))

    def test_crop_square_image(self):
        image = np.array([
            [20, 10, 20, 10, 20, 10],
            [10, 20, 10, 20, 10, 20],
            [20, 10, 20, 10, 20, 10],
            [10, 20, 10, 20, 10, 20]
        ], dtype=np.uint8).reshape(4, 6, 1)

        image = np.repeat(image, 3, axis=2)
        result = ImageService.crop_square_image(image)

        expected_cropped_image = np.array([
            [10, 20, 10, 20],
            [20, 10, 20, 10],
            [10, 20, 10, 20],
            [20, 10, 20, 10],
        ], dtype=np.uint8).reshape(4, 4, 1)

        expected_cropped_image = np.repeat(expected_cropped_image, 3, axis=2)
        self.assertTrue(result.is_success)
        np.testing.assert_array_equal(result.value, expected_cropped_image)
