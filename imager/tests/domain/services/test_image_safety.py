import unittest

import cv2
import numpy as np

from src.domain.image_builder.services.image_service import ImageService
from src.domain.image_builder.value_objects.image_build_params import (
    AlphaChannel, CellSize, ImageGroup, NoiseLevel, ResultSize,
)


class TestImageSafety(unittest.TestCase):
    def test_grayscale_normalization_replicates_channels(self):
        gray = np.array([[0, 127], [255, 64]], dtype=np.uint8)
        result = ImageService.normalize_image(gray)
        self.assertTrue(result.is_success)
        self.assertEqual(result.value.dtype, np.uint8)
        self.assertEqual(result.value.shape, (2, 2, 3))
        for channel in range(3):
            np.testing.assert_array_equal(result.value[:, :, channel], gray)

    def test_bgra_normalization_keeps_bgr_channels(self):
        source = np.array([[[3, 20, 250, 0]]], dtype=np.uint8)
        result = ImageService.normalize_image(source)
        self.assertTrue(result.is_success)
        np.testing.assert_array_equal(result.value, source[:, :, :3])

    def test_uint16_normalization_uses_full_range(self):
        source = np.array([[0, 32768, 65535]], dtype=np.uint16)
        result = ImageService.normalize_image(source)
        self.assertTrue(result.is_success)
        np.testing.assert_array_equal(result.value[0, :, 0], [0, 128, 255])

    def test_invalid_pixels_are_rejected(self):
        for image in (
            None,
            np.empty((0, 2, 3), dtype=np.uint8),
            np.zeros((2, 2, 2), dtype=np.uint8),
            np.array([[np.nan]], dtype=np.float32),
        ):
            with self.subTest(image=type(image)):
                self.assertFalse(ImageService.normalize_image(image).is_success)

    def test_source_and_catalog_aspect_policies_are_separate(self):
        panorama = np.zeros((20, 100, 3), dtype=np.uint8)
        self.assertTrue(ImageService.validate_image(panorama).is_success)
        self.assertFalse(ImageService.validate_image(
            panorama, aspect_ratio_limits=(9 / 16, 16 / 9)
        ).is_success)
        self.assertFalse(ImageService.validate_image(
            panorama, max_size=(80, 80)
        ).is_success)

    def test_downscale_to_one_pixel_represents_whole_image(self):
        source = np.full((6, 6, 3), 255, dtype=np.uint8)
        source[2:4, 2:4] = 0
        result = ImageService.resize_image(source, 1, 1)
        self.assertTrue(result.is_success)
        self.assertEqual(int(result.value[0, 0, 0]), 227)
        self.assertAlmostEqual(
            int(result.value[0, 0, 0]),
            ImageService.average_color(source).value[0], delta=1,
        )

    def test_crop_and_whole_image_mean_have_different_meaning(self):
        source = np.full((2, 6, 3), 255, dtype=np.uint8)
        source[:, 2:4] = 0
        self.assertEqual(ImageService.average_color(source).value, (170, 170, 170))
        cropped = ImageService.crop_square_image(source).value
        self.assertEqual(ImageService.average_color(cropped).value, (0, 0, 0))
        self.assertEqual(
            tuple(ImageService.resize_image(source, 1, 1).value[0, 0]),
            (170, 170, 170),
        )

    def test_chunked_overlay_matches_reference_without_extra_output(self):
        generator = np.random.default_rng(9)
        background = generator.integers(0, 256, (9, 13, 3), dtype=np.uint8)
        overlay = generator.integers(0, 256, (3, 5, 3), dtype=np.uint8)
        expected = cv2.addWeighted(
            background, 0.7, cv2.resize(overlay, (13, 9)), 0.3, 0
        )
        result = ImageService.overlay_image_alpha(
            background, overlay, 0.3, inplace=True, chunk_rows=2
        )
        self.assertTrue(result.is_success)
        self.assertIs(result.value, background)
        np.testing.assert_array_equal(result.value, expected)

    def test_default_overlay_preserves_background(self):
        background = np.zeros((2, 3, 3), dtype=np.uint8)
        overlay = np.full((2, 3, 3), 200, dtype=np.uint8)
        result = ImageService.overlay_image_alpha(background, overlay, 0.5)
        self.assertTrue(result.is_success)
        self.assertTrue(np.all(background == 0))
        self.assertTrue(np.all(result.value == 100))

    def test_overlay_extremes(self):
        background = np.full((2, 3, 3), 20, dtype=np.uint8)
        overlay = np.full((2, 3, 3), 220, dtype=np.uint8)
        for alpha, expected in ((0, 20), (1, 220)):
            result = ImageService.overlay_image_alpha(background, overlay, alpha)
            self.assertTrue(result.is_success)
            self.assertTrue(np.all(result.value == expected))

    def test_uneven_splits_cover_every_pixel_once(self):
        image = np.arange(3 * 5 * 3, dtype=np.uint8).reshape(3, 5, 3)
        result = ImageService.split_image(image, 2, 2)
        self.assertTrue(result.is_success)
        self.assertEqual(len(result.value), 4)
        reconstructed = np.concatenate((
            np.concatenate(result.value[:2], axis=1),
            np.concatenate(result.value[2:], axis=1),
        ), axis=0)
        np.testing.assert_array_equal(reconstructed, image)

    def test_nonpositive_resize_dimensions_are_rejected(self):
        image = np.zeros((2, 2, 3), dtype=np.uint8)
        self.assertFalse(ImageService.resize_image(image, 1, 0).is_success)
        self.assertFalse(ImageService.resize_image(image, True, 1).is_success)


class TestParameterFactorySafety(unittest.TestCase):
    def test_bool_is_not_a_numeric_parameter(self):
        for factory in (AlphaChannel, NoiseLevel, CellSize, ResultSize):
            with self.subTest(factory=factory.__name__):
                self.assertFalse(factory.create(True).is_success)

    def test_safety_caps_are_not_preset_values(self):
        self.assertTrue(CellSize.create(537).is_success)
        self.assertTrue(ResultSize.create(471).is_success)
        self.assertTrue(CellSize.create(CellSize.MAX_VALUE).is_success)
        self.assertTrue(ResultSize.create(ResultSize.MAX_VALUE).is_success)
        self.assertFalse(CellSize.create(CellSize.MAX_VALUE + 1).is_success)
        self.assertFalse(ResultSize.create(ResultSize.MAX_VALUE + 1).is_success)

    def test_public_constructors_remain_available(self):
        self.assertEqual(CellSize(99999).value, 99999)
        self.assertEqual(ResultSize(99999).value, 99999)

    def test_whitespace_group_is_rejected(self):
        self.assertFalse(ImageGroup.create('   ').is_success)
