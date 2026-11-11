import unittest

from pydantic import ValidationError
from settings import Settings


class TestSettingsSafety(unittest.TestCase):
    @staticmethod
    def config(**values):
        return Settings(
            _env_file=None, mongodb_uri='mongodb://127.0.0.1:1',
            database_name='offline_tests', **values,
        )

    def test_invalid_resource_configuration_is_rejected(self):
        invalid = (
            {'generated_image_jpeg_quality': 101},
            {'generation_estimated_jpeg_bytes_per_pixel': -1},
            {'generation_estimated_jpeg_bytes_per_pixel': float('nan')},
            {'telegram_max_document_size_bytes': -1},
            {'rabbitmq_prefetch_count': 0},
            {'api_queue_maxsize': 0},
            {'aspect_ratio_limits': (2, 1)},
            {'max_size': (8000, 4000)},
            {'min_size': (0, 20)},
        )
        for values in invalid:
            with self.subTest(values=values), self.assertRaises(ValidationError):
                self.config(**values)

    def test_valid_policy_and_runtime_queue_settings(self):
        config = self.config(api_queue_maxsize=4, generated_image_jpeg_quality=85)
        self.assertEqual(config.api_queue_maxsize, 4)
        self.assertEqual(config.generated_image_jpeg_quality, 85)
