"""Second bot credentials stay optional, separate and masked."""
import importlib.util
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from pydantic import ValidationError


class SupportSettingsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(__file__).resolve().parents[1] / 'settings.py'
        spec = importlib.util.spec_from_file_location('support_settings_model', path)
        module = importlib.util.module_from_spec(spec)
        # The normal offline suite supplies a lightweight settings namespace.
        # Load the actual Settings model independently with fake credentials.
        with patch.dict(os.environ, {
            'API_TOKEN': 'offline-main-token', 'SUPPORT_BOT_TOKEN': '',
            'MONGODB_URI': 'mongodb://127.0.0.1:1', 'TELEGRAM_ADMIN_IDS': '1',
        }):
            spec.loader.exec_module(module)
        cls.Settings = module.Settings

    def settings(self, **values):
        return self.Settings(
            _env_file=None, api_token='offline-main-token', mongodb_uri='mongodb://127.0.0.1:1',
            telegram_admin_ids=(1,), **values,
        )

    def test_blank_support_token_disables_the_second_bot(self):
        for value in (None, '', '   '):
            with self.subTest(value=value):
                self.assertIsNone(self.settings(support_bot_token=value).support_bot_token)

    def test_support_credential_is_separate_and_masked(self):
        configured = self.settings(support_bot_token='offline-support-token')
        self.assertEqual(configured.support_bot_token.get_secret_value(), 'offline-support-token')
        for secret in ('offline-main-token', 'offline-support-token'):
            self.assertNotIn(secret, repr(configured))
            self.assertNotIn(secret, str(configured))

    def test_reusing_the_main_token_is_rejected_without_printing_secrets(self):
        with self.assertRaises(ValidationError) as error:
            self.settings(support_bot_token='offline-main-token')
        self.assertIn('different bot token', str(error.exception))
        self.assertNotIn('offline-main-token', str(error.exception))
