from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from src.support.config import load_support_config, support_config


class SupportConfigTests(unittest.TestCase):
    def test_default_limits_are_bot_local_and_bounded(self):
        self.assertEqual(support_config.max_pending_per_user, 5)
        self.assertEqual(support_config.message_cooldown_seconds, 10)
        self.assertEqual(support_config.rate_window_seconds, 600)
        self.assertEqual(support_config.max_messages_per_window, 5)
        self.assertEqual(support_config.max_messages_per_day, 20)
        self.assertEqual(support_config.notice_cooldown_seconds, 60)
        self.assertEqual(support_config.max_pending_total, 200)
        self.assertEqual(support_config.max_stored_events, 5000)
        self.assertEqual(support_config.retention_seconds, 30 * 24 * 60 * 60)

    def test_invalid_partial_unknown_or_unbounded_display_configuration_is_rejected(self):
        defaults = asdict(support_config)
        for change in ({'poll_seconds': 0}, {'retention_seconds': True}, {'collection': '../users'},
                       {'max_text_length': 4097}, {'max_pending_per_user': 201},
                       {'max_stored_events': 200}, {'retention_seconds': 10 ** 30}, {'unknown': 1},
                       {'message_cooldown_seconds': 601}, {'max_messages_per_window': 21},
                       {'retention_seconds': 600}, {'max_messages_per_day': True},
                       {'notice_cooldown_seconds': 0}, {'rate_window_seconds': 86401},
                       {'notice_cooldown_seconds': support_config.retention_seconds + 1}):
            with self.subTest(change=change), TemporaryDirectory() as directory:
                values = {**defaults, **change}
                path = Path(directory) / 'support.toml'
                path.write_text('\n'.join(f'{key} = {repr(value).lower() if isinstance(value, bool) else repr(value)}'
                                          for key, value in values.items()), encoding='utf-8')
                with self.assertRaises(ValueError):
                    load_support_config(path)
