import base64
import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


SETUP = Path(__file__).resolve().parents[3] / 'infra' / 'setup.py'
spec = importlib.util.spec_from_file_location('imager_deployment_setup', SETUP)
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)


class TestDeploymentSetup(unittest.TestCase):
    def test_generated_auth_and_credentials_are_stable_and_project_local(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = root / 'dedicated-data'
            result = setup.prepare(root, data)
            credentials = root / '.local' / 'secrets' / 'ui-credentials.json'
            credential = json.loads(credentials.read_text(encoding='utf-8'))
            hashed = (root / '.local/secrets/temporal-ui.htpasswd').read_text().strip().split(':', 1)[1]
            raw = base64.b64decode(hashed.removeprefix('{SSHA}'))
            self.assertEqual(hashlib.sha1(credential['password'].encode() + raw[20:]).digest(), raw[:20])
            self.assertGreaterEqual(len(credential['password']), 32)
            self.assertEqual(result['data'], data)
            before = credentials.read_bytes()
            setup.prepare(root)
            self.assertEqual(credentials.read_bytes(), before)
            self.assertTrue(all((data / name).is_dir() for name in (
                'mongo', 'minio', 'temporal', 'scratch', 'bot-scratch', 'client-tmp',
            )))

    def test_existing_secrets_keep_dollar_characters_literal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'infra').mkdir()
            (root / 'infra/.env').write_text("MONGO_PASSWORD='abc$literal-secret'\n", encoding='utf-8')
            setup.prepare(root, root / 'runtime')
            text = (root / 'infra/.env').read_text(encoding='utf-8')
            self.assertIn("MONGO_PASSWORD='abc$literal-secret'", text)
            self.assertEqual(setup.parse_env(root / 'infra/.env')['MONGO_PASSWORD'], 'abc$literal-secret')

    def test_weak_ui_password_is_not_written(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(ValueError):
                setup.prepare(root, root / 'runtime', ui_password='short')
            self.assertFalse((root / '.local/secrets/temporal-ui.htpasswd').exists())

    def test_relative_env_data_root_uses_compose_infra_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'infra').mkdir()
            (root / 'infra/.env').write_text('IMAGER_DATA_ROOT=../.local/data\n', encoding='utf-8')
            result = setup.prepare(root)
            expected = (root / '.local/data').resolve()
            self.assertEqual(result['data'], expected)
            self.assertEqual(setup.parse_env(root / 'infra/.env')['IMAGER_DATA_ROOT'], expected.as_posix())
            self.assertTrue((expected / 'client-tmp').is_dir())
