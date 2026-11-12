import base64
import hashlib
import importlib.util
import json
import re
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from infra.certificates import ensure_certificates, find_openssl


SETUP = Path(__file__).resolve().parents[3] / 'infra' / 'setup.py'
spec = importlib.util.spec_from_file_location('imager_deployment_setup', SETUP)
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)


class TestDeploymentSetup(unittest.TestCase):
    def test_production_namespace_uses_same_sdk_image_and_tls_environment_as_worker(self):
        compose = (SETUP.parent / 'docker-compose.prod.yaml').read_text(encoding='utf-8')
        namespace = compose.split('  temporal-namespace:\n', 1)[1].split('  temporal-web:\n', 1)[0]
        worker = compose.split('  imager-worker:\n', 1)[1].split('  telegram-bot:\n', 1)[0]
        image = lambda block: re.search(r'^    image: (.+)$', block, re.MULTILINE).group(1)
        self.assertEqual(image(namespace), image(worker))
        self.assertIn('environment: *core-environment', namespace)
        self.assertIn('secrets: *client-tls-secrets', namespace)
        self.assertIn('- *config-volume', namespace)
        self.assertIn('- src.presentation.temporal.namespace', namespace)
        self.assertNotIn('TEMPORAL_TLS_', namespace)

    def test_production_credentials_and_tls_are_separate_and_idempotent(self):
        try:
            openssl = find_openssl()
        except ValueError:
            self.skipTest('OpenSSL is not installed')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            development = setup.prepare(root, root / 'runtime')
            development_bytes = development['env'].read_bytes()
            production_config = replace(setup.config.deployment.production, rsa_key_bits=2048)
            configured = replace(setup.config, deployment=replace(
                setup.config.deployment, production=production_config,
            ))
            with patch.object(setup, 'config', configured):
                production = setup.prepare(root, production=True, openssl=openssl)
                values = setup.parse_env(production['env'])
                production_bytes = production['env'].read_bytes()
                certificate = Path(values['IMAGER_TLS_SERVER_CERT_FILE']).read_bytes()
                again = setup.prepare(root, production=True, openssl=openssl)
            self.assertEqual(development['env'].read_bytes(), development_bytes)
            self.assertEqual(production['data'], root / 'runtime' / production_config.data_directory)
            self.assertEqual(again, production)
            self.assertEqual(Path(values['IMAGER_TLS_SERVER_CERT_FILE']).read_bytes(), certificate)
            self.assertNotEqual(values['MONGO_PASSWORD'], setup.parse_env(development['env'])['MONGO_PASSWORD'])
            self.assertNotEqual(values['MINIO_SECRET_KEY'], setup.parse_env(development['env'])['MINIO_SECRET_KEY'])
            self.assertEqual(production['env'].read_bytes(), production_bytes)
            self.assertFalse(any(production_config.ca_private_key in value for value in values.values()))
            self.assertTrue(Path(values['IMAGER_POSTGRES_DATA_PATH']).is_dir())
            server_key = Path(values['IMAGER_TLS_SERVER_KEY_FILE'])
            original_key = server_key.read_bytes()
            server_key.write_bytes(Path(values['IMAGER_TLS_CLIENT_KEY_FILE']).read_bytes())
            try:
                with self.assertRaisesRegex(ValueError, 'does not match'):
                    ensure_certificates(server_key.parent, production_config, openssl)
            finally:
                server_key.write_bytes(original_key)

    def test_production_cannot_reuse_development_data(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            development = setup.prepare(root, root / 'runtime')
            before = development['env'].read_bytes()
            with self.assertRaisesRegex(ValueError, 'overlap'):
                setup.prepare(root, root / 'runtime', production=True)
            self.assertEqual(development['env'].read_bytes(), before)

    def test_incomplete_production_tls_is_not_silently_replaced(self):
        with tempfile.TemporaryDirectory() as directory:
            policy = setup.config.deployment.production
            root = Path(directory)
            certificate = root / policy.ca_certificate
            certificate.write_text('existing CA', encoding='ascii')
            with patch('infra.certificates.find_openssl', return_value='openssl'):
                with self.assertRaisesRegex(ValueError, 'incomplete'):
                    ensure_certificates(root, policy)
            self.assertEqual(certificate.read_text(), 'existing CA')

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
