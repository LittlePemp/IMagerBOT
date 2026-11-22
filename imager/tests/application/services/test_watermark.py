import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import cv2
import numpy as np

from imager_config.loader import ConfigurationError, config, load_config
from src.application.image_builder.services import watermark


class WatermarkTests(unittest.TestCase):
    def test_legacy_none_preserves_object_pixels_and_does_not_allocate(self):
        image = np.full((128, 128, 3), 123, dtype=np.uint8)
        with patch.object(watermark, 'require_memory') as reserve:
            self.assertIs(watermark.apply_watermark(image, None), image)
        reserve.assert_not_called()
        self.assertTrue(np.all(image == 123))

    def test_normal_square_portrait_and_landscape_are_in_place_and_local_to_bottom_right(self):
        for height, width in ((1024, 1024), (1024, 576), (576, 1024)):
            with self.subTest(shape=(height, width)):
                image = np.full((height, width, 3), 175, dtype=np.uint8)
                original = image.copy()
                with patch.object(watermark, 'require_memory') as reserve:
                    result = watermark.apply_watermark(image, 't.me/ExampleBot')
                self.assertIs(result, image)
                self.assertEqual(result.shape, original.shape)
                np.testing.assert_array_equal(result[:height // 2], original[:height // 2])
                np.testing.assert_array_equal(result[:, :width // 2], original[:, :width // 2])
                self.assertFalse(np.array_equal(result, original))
                self.assertLess(max(call.args[0] for call in reserve.call_args_list), original.nbytes // 4)

    def test_text_keeps_contrast_on_white_and_black_backgrounds(self):
        for background in (0, 255):
            with self.subTest(background=background):
                image = np.full((512, 512, 3), background, dtype=np.uint8)
                result = watermark.apply_watermark(image, 't.me/ExampleBot')
                self.assertEqual(result.min(), 0)
                self.assertEqual(result.max(), 255)
                self.assertTrue(np.any(result != background))

    def test_tiny_mosaic_is_retained_and_complete_link_fits_in_an_added_footer(self):
        for height, width in ((1, 1), (9, 12), (40, 40)):
            with self.subTest(shape=(height, width)):
                image = np.full((height, width, 3), (25, 75, 125), dtype=np.uint8)
                original = image.copy()
                with patch.object(watermark.cv2, 'putText', wraps=cv2.putText) as draw:
                    result = watermark.apply_watermark(image, 't.me/ExampleBot')
                self.assertGreater(result.shape[0], height)
                self.assertGreaterEqual(result.shape[1], width)
                np.testing.assert_array_equal(result[:height, :width], original)
                self.assertEqual([call.args[1] for call in draw.call_args_list],
                                 ['t.me/ExampleBot', 't.me/ExampleBot'])
                for call in draw.call_args_list:
                    roi, text, origin, font, scale, color, thickness, line = call.args
                    (text_width, text_height), baseline = cv2.getTextSize(text, font, scale, thickness)
                    self.assertGreaterEqual(origin[0], 0)
                    self.assertLessEqual(origin[0] + text_width, roi.shape[1])
                    self.assertGreaterEqual(origin[1] - text_height, 0)
                    self.assertLessEqual(origin[1] + baseline, roi.shape[0])

    def test_watermark_text_is_validated_before_geometry_or_allocation(self):
        for text in ('', 'bad\ntext', 'бот', 'x' * 129, 7, True):
            with self.subTest(text=repr(text)), patch.object(watermark, '_geometry') as geometry:
                with self.assertRaises(ValueError):
                    watermark.apply_watermark(None, text)
                geometry.assert_not_called()

    def test_invalid_pixels_are_rejected_without_allocating(self):
        for image in (None, np.zeros((0, 1, 3), dtype=np.uint8), np.zeros((8, 8), dtype=np.uint8),
                      np.zeros((8, 8, 4), dtype=np.uint8), np.zeros((8, 8, 3), dtype=np.float32),
                      np.zeros((8, 8, 3), dtype=np.uint8)[:, ::2]):
            with self.subTest(shape=getattr(image, 'shape', None)), patch.object(watermark, 'require_memory') as reserve:
                with self.assertRaises(ValueError):
                    watermark.apply_watermark(image, 't.me/ExampleBot')
                reserve.assert_not_called()

    def test_expanded_footer_checks_pixel_and_memory_budget_before_allocating(self):
        image = np.full((1, 1, 3), 90, dtype=np.uint8)
        limited = replace(config, limits=replace(config.limits, output_pixels=1))
        with patch.object(watermark, 'config', limited), patch.object(watermark.cv2, 'copyMakeBorder') as allocate:
            with self.assertRaisesRegex(ValueError, 'pixel budget'):
                watermark.apply_watermark(image, 't.me/ExampleBot')
            allocate.assert_not_called()
        with patch.object(watermark, 'require_memory', side_effect=MemoryError('budget')), \
                patch.object(watermark.cv2, 'copyMakeBorder') as allocate:
            with self.assertRaises(MemoryError):
                watermark.apply_watermark(image, 't.me/ExampleBot')
            allocate.assert_not_called()
        np.testing.assert_array_equal(image, np.full((1, 1, 3), 90, dtype=np.uint8))

    def test_maximum_preset_layout_has_bounded_font_and_roi_without_full_image_allocation(self):
        metrics, margin, width, height = watermark._geometry(
            SimpleNamespace(shape=(5120, 5120, 3)), 't.me/ExampleBot', config.watermark,
        )
        _, _, _, text_width, text_height, baseline, padding = metrics
        self.assertEqual((height, width), (5120, 5120))
        self.assertGreater(margin, config.watermark.min_margin_pixels)
        self.assertLess((text_width + 2 * padding) * (text_height + baseline + 2 * padding) * 6, 2_000_000)


class WatermarkConfigurationTests(unittest.TestCase):
    def test_invalid_style_is_rejected_and_partial_override_preserves_other_sections(self):
        invalid = (
            'font_height_fraction=0', 'preferred_width_fraction=1.1', 'margin_fraction=0.5',
            'background_opacity=1.1', 'stroke_fraction=0', 'min_font_height_pixels=73',
            'max_font_height_pixels=4001', 'padding_pixels=1', 'outline_pixels=7',
            'foreground_bgr=[255,255]', 'foreground_bgr=[256,0,0]',
            'foreground_bgr=[0,0,0]', 'background_bgr=[false,0,0]',
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'watermark.toml'
            for setting in invalid:
                with self.subTest(setting=setting):
                    path.write_text('[watermark]\n' + setting, encoding='utf-8')
                    with self.assertRaises(ConfigurationError):
                        load_config(path)
            path.write_text('[watermark]\nbackground_opacity=0\nfont_height_fraction=0.025\n', encoding='utf-8')
            changed = load_config(path)
        self.assertEqual(changed.watermark.background_opacity, 0)
        self.assertEqual(changed.watermark.font_height_fraction, 0.025)
        self.assertEqual(changed.limits, config.limits)
