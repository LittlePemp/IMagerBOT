import importlib.util
import io
from pathlib import Path
import unittest
import tempfile
import os
import sys
from contextlib import redirect_stdout
from unittest.mock import patch


class CloudMigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        script = Path(__file__).resolve().parents[2] / 'infra' / 'telegram_api.py'
        spec = importlib.util.spec_from_file_location('imager_telegram_migration', script)
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)

    def test_only_explicit_logout_method_is_called_without_sending_messages(self):
        calls = []
        def opener(request, **kwargs):
            calls.append(request)
            return io.BytesIO(b'{"ok":true,"result":true}')
        self.module.logout_cloud('123:dummy_token', opener=opener)
        self.assertEqual(calls[0].get_method(), 'POST')
        self.assertTrue(calls[0].full_url.endswith('/logOut'))

    def test_credentials_are_not_repeated_in_failure_messages(self):
        def opener(request, **kwargs):
            raise OSError(f'private endpoint {request.full_url}')
        with self.assertRaises(RuntimeError) as caught:
            self.module.logout_cloud('123:private_token', opener=opener)
        self.assertNotIn('private_token', str(caught.exception))

    def test_explicit_project_file_wins_over_another_projects_shell_token(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'project.env'
            path.write_text('API_TOKEN=123:project_token\n', encoding='utf-8')
            with patch.dict(os.environ, {'API_TOKEN': '999:other_project'}), patch.object(
                sys, 'argv', ['telegram_api.py', '--env-file', str(path), '--logout-cloud'],
            ), patch.object(self.module, 'logout_cloud') as logout, redirect_stdout(io.StringIO()):
                self.module.main()
            logout.assert_called_once_with('123:project_token')
