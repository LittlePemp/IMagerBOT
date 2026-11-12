import base64
import hashlib
import importlib.util
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch


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

    def test_toml_paths_and_auth_filenames_are_used_in_generated_compose_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            configured = replace(setup.config, paths=replace(
                setup.config.paths, data_root='state', secrets_root='.keys',
                ui_credentials='ui.json', ui_password_hash='ui.auth',
                readable_credentials='credentials.txt', mongo_directory='database',
                worker_scratch_directory='worker-temp', bot_scratch_directory='bot-temp',
            ), deployment=replace(setup.config.deployment, temporal_ui_port=18088))
            with patch.object(setup, 'config', configured):
                result = setup.prepare(root)
            self.assertEqual(result['data'], root / 'state')
            self.assertTrue((root / '.keys/ui.json').is_file())
            self.assertTrue((root / '.keys/ui.auth').is_file())
            self.assertEqual(result['credentials'], root / '.keys/credentials.txt')
            env = setup.parse_env(result['env'])
            self.assertEqual(env['TEMPORAL_UI_PORT'], '18088')
            self.assertEqual(env['IMAGER_MONGO_DATA_PATH'], (root / 'state/database').as_posix())
            self.assertEqual(env['IMAGER_WORKER_SCRATCH_PATH'], (root / 'state/worker-temp').as_posix())
            self.assertEqual(env['IMAGER_TEMPORAL_UI_AUTH_FILE'], (root / '.keys/ui.auth').as_posix())
