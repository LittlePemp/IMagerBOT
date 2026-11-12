"""Import scratch is configured independently from renderer and Telegram files."""
import importlib.util
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch


SETUP = Path(__file__).resolve().parents[3] / 'infra' / 'setup.py'
spec = importlib.util.spec_from_file_location('pack_import_deployment_setup', SETUP)
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)


class CatalogImportDeploymentTests(unittest.TestCase):
    def test_import_scratch_has_its_own_configured_directory_and_preserves_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = root / 'runtime'
            (root / 'infra').mkdir()
            (root / 'infra' / '.env').write_text(
                "TELEGRAM_API_ID='12345'\nTELEGRAM_API_HASH='operator-owned-app-hash'\n",
                encoding='utf-8',
            )
            configured = replace(setup.config, paths=replace(
                setup.config.paths, catalog_import_scratch_directory='pack-temp',
                bot_api_directory='telegram-api-data', bot_api_temp_directory='telegram-api-temp',
            ))
            with patch.object(setup, 'config', configured):
                first = setup.prepare(root, data)
                credential_path = first['credentials']
                credentials = credential_path.read_bytes()
                second = setup.prepare(root)
            values = setup.parse_env(second['env'])
            self.assertEqual(values['IMAGER_IMPORT_SCRATCH_PATH'], (data / 'pack-temp').as_posix())
            self.assertTrue((data / 'pack-temp').is_dir())
            self.assertNotEqual(values['IMAGER_IMPORT_SCRATCH_PATH'], values['IMAGER_WORKER_SCRATCH_PATH'])
            self.assertNotEqual(values['IMAGER_IMPORT_SCRATCH_PATH'], values['IMAGER_BOT_SCRATCH_PATH'])
            self.assertEqual(values['IMAGER_BOT_API_DATA_PATH'], (data / 'telegram-api-data').as_posix())
            self.assertEqual(values['IMAGER_BOT_API_TEMP_PATH'], (data / 'telegram-api-temp').as_posix())
            self.assertTrue((data / 'telegram-api-data').is_dir())
            self.assertTrue((data / 'telegram-api-temp').is_dir())
            self.assertEqual(values['TELEGRAM_API_ID'], '12345')
            self.assertEqual(values['TELEGRAM_API_HASH'], 'operator-owned-app-hash')
            self.assertEqual(credential_path.read_bytes(), credentials)
