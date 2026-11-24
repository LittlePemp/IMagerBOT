import tempfile
import unittest
from pathlib import Path

from imager_config.loader import ConfigurationError, config, load_config
from imager_config.models import MAX_DAILY_ADMISSION_RECEIPTS


class AdmissionPolicyConfigurationTests(unittest.TestCase):
    def test_attempt_budget_is_separate_from_success_quota_and_bson_bounded(self):
        self.assertEqual((config.bot.regular_user_upload_limit, config.bot.max_daily_admissions), (5, 64))
        for value in (config.bot.regular_user_upload_limit - 1, MAX_DAILY_ADMISSION_RECEIPTS + 1, True):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as directory:
                override = Path(directory) / 'override.toml'
                literal = str(value).lower()
                override.write_text(f'[bot]\nmax_daily_admissions={literal}\n', encoding='utf-8')
                with self.assertRaises(ConfigurationError):
                    load_config(override)
