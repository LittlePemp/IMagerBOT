import unittest

from imager_config.loader import config
from src.application.image_errors import (
    BUILD_FAILED_TEXT, IMAGE_TOO_NARROW_TEXT, IMAGE_TOO_WIDE_TEXT,
    format_generation_error, image_aspect_error,
)


class ImageErrorTests(unittest.TestCase):
    def test_reported_narrow_image_and_wide_image_have_actionable_copy(self):
        cases = (
            ('Invalid image: Image aspect ratio 0.4594 must be between 0.5625 and 1.7778', IMAGE_TOO_NARROW_TEXT),
            ('Invalid image: Image aspect ratio 1.9000 must be between 0.5625 and 1.7778', IMAGE_TOO_WIDE_TEXT),
        )
        for error, expected in cases:
            with self.subTest(error=error):
                text = format_generation_error({'error': error})
                self.assertEqual(text, expected)
                self.assertIn('квадрата', text)
                self.assertNotIn('0.5625', text)
                self.assertNotIn('Invalid image', text)

    def test_four_decimal_rounding_at_rejected_boundaries_still_explains_direction(self):
        for ratio, expected in (('0.5625', IMAGE_TOO_NARROW_TEXT), ('1.7778', IMAGE_TOO_WIDE_TEXT)):
            with self.subTest(ratio=ratio):
                self.assertEqual(format_generation_error({
                    'error': f'Invalid image: Image aspect ratio {ratio} must be between 0.5625 and 1.7778',
                }), expected)

    def test_only_exact_known_core_diagnostic_is_translated(self):
        for error in (
            None, {'message': 'secret'}, 'mongodb://private:password@host/private-id',
            'Invalid image',
            'Traceback: Invalid image: Image aspect ratio 0.4594 must be between 0.5625 and 1.7778',
            'Invalid image: Image aspect ratio 0.4594 must be between 0.5625 and 1.7778\nprivate-id',
            'Invalid image: Image aspect ratio nan must be between 0.5625 and 1.7778',
            'Invalid image: Image aspect ratio 0.4 must be between 2.0 and 1.0',
            'Invalid image: Image aspect ratio 1.0 must be between 1.0 and 1.0',
            'Invalid image: Image aspect ratio 1.0 must be between 0.5 and 2.0',
            'private-id ' + 'x' * config.bot.error_max_length,
        ):
            with self.subTest(error=error):
                self.assertEqual(format_generation_error({'error': error}), BUILD_FAILED_TEXT)

    def test_metadata_preflight_uses_current_bounds_without_rejecting_exact_boundaries(self):
        self.assertEqual(image_aspect_error(4594, 10000), IMAGE_TOO_NARROW_TEXT)
        self.assertEqual(image_aspect_error(1900, 1000), IMAGE_TOO_WIDE_TEXT)
        self.assertIsNone(image_aspect_error(9, 16))
        self.assertIsNone(image_aspect_error(16, 9))
        self.assertIsNone(image_aspect_error(512, 512))

    def test_missing_or_invalid_metadata_is_left_to_the_bounded_decoder(self):
        for width, height in ((None, 512), (512, None), (True, 512), (512, False),
                              (0, 512), (512, -1), ('512', 512), (10 ** 500, 1)):
            with self.subTest(width=width, height=height):
                self.assertIsNone(image_aspect_error(width, height))
