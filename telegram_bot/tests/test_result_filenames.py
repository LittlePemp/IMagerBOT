"""Public download filenames never disclose the generation identity."""
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from imager_config.loader import config
from src.utils import result_files


PRIVATE_ID = 'eac94427-9dc9-5b5b-b55c-2bf4d891eb31'
CREATED = datetime(2026, 11, 12, 13, 14, 15, 123456, tzinfo=timezone.utc)


class PublicResultFilenameTests(unittest.TestCase):
    def test_filename_uses_nickname_timestamp_and_independent_random_suffix(self):
        with patch.object(result_files.secrets, 'token_hex', return_value='ab' * 32):
            name = result_files.new_result_filename('@Alice', created_at=CREATED)
        expected = config.render.output_filename_template.format(
            stem='Alice', timestamp=CREATED.strftime(config.render.output_timestamp_format),
            suffix=('ab' * 32)[:config.render.output_random_suffix_length], extension=config.render.output_extension,
        )
        self.assertEqual(name, expected)
        self.assertNotIn(PRIVATE_ID, name)
        self.assertNotIn(PRIVATE_ID.replace('-', ''), name)

    def test_missing_or_unsafe_nickname_has_a_safe_basename(self):
        for nickname in (None, '', '../Alice\\pictures', PRIVATE_ID, 123):
            with self.subTest(nickname=nickname):
                name = result_files.new_result_filename(nickname, created_at=CREATED)
                self.assertNotIn('/', name)
                self.assertNotIn('\\', name)
                self.assertFalse(name.startswith('.'))
                self.assertFalse(result_files.contains_generation_reference(name))
                self.assertTrue(name.endswith('.' + config.render.output_extension))

    def test_new_names_do_not_collide_at_the_same_timestamp(self):
        with patch.object(result_files.secrets, 'token_hex', side_effect=['ab' * 32, 'cd' * 32]):
            first = result_files.new_result_filename('Alice', created_at=CREATED)
            second = result_files.new_result_filename('Alice', created_at=CREATED)
        self.assertNotEqual(first, second)

    def test_full_frozen_filename_is_kept_without_a_second_extension(self):
        filename = result_files.new_result_filename('Alice', created_at=CREATED)
        self.assertEqual(
            result_files.delivery_result_filename(filename, job_id=PRIVATE_ID), filename,
        )
        self.assertFalse(filename.endswith('.jpg.jpg'))

    def test_legacy_names_and_paths_fall_back_to_config_and_creation_date(self):
        for legacy in (
            None, '', f'imager_{PRIVATE_ID}', f'imager_{PRIVATE_ID}.jpg',
            f'imager_{PRIVATE_ID.replace("-", "")}.jpg', '../result.jpg',
            r'C:\tmp\result.jpg', 'result.jpg.jpg', 'result.exe',
        ):
            with self.subTest(legacy=legacy):
                name = result_files.delivery_result_filename(legacy, created_at=CREATED, job_id=PRIVATE_ID)
                self.assertTrue(name.startswith(config.render.output_stem))
                self.assertIn(CREATED.strftime(config.render.output_timestamp_format), name)
                self.assertFalse(result_files.contains_generation_reference(name, job_id=PRIVATE_ID))
                self.assertTrue(name.endswith('.' + config.render.output_extension))
