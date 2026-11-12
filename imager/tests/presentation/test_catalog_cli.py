import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import cv2
import numpy as np
from click.testing import CliRunner
from imager_config.loader import load_config
from src.infrastructure.object_storage.catalog import preview_local_catalog
from src.presentation.cli.catalog_cli import cli


class TestCatalogCli(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='imager-cli-test-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.group = self.root / 'cats'
        self.group.mkdir()
        cv2.imwrite(str(self.group / 'one.png'), np.full((20, 20, 3), 80, dtype=np.uint8))

    def test_dry_run_uses_no_database_or_object_store_and_reports_capacity(self):
        with patch('pymongo.MongoClient') as mongo, patch(
            'src.infrastructure.object_storage.minio_store.object_store_from_settings',
        ) as store:
            result = CliRunner().invoke(cli, [str(self.root), '--dry-run'])
        self.assertEqual(result.exit_code, 0, result.output)
        mongo.assert_not_called()
        store.assert_not_called()
        report = json.loads(result.output)
        self.assertTrue(report['dry_run'])
        self.assertEqual(report['totals']['count'], 1)
        self.assertGreater(report['totals']['additional_payload_bytes_upper_bound'], 0)

    def test_import_uses_shared_collection_and_bounded_mongo_timeouts(self):
        override = self.root / 'override.toml'
        override.write_text('[mongo]\ncatalog_collection="catalog_v2"\n', encoding='utf-8')
        changed = load_config(override)
        client = Mock()
        database = Mock()
        client.__enter__ = Mock(return_value=client)
        client.__exit__ = Mock(return_value=False)
        client.__getitem__ = Mock(return_value=database)
        database.__getitem__ = Mock(return_value='selected collection')
        settings = SimpleNamespace(mongodb_uri='mongodb://unused', database_name='core')
        preview = preview_local_catalog(self.root)
        with patch('imager_config.loader.config', changed), patch('settings.settings', settings), patch(
            'pymongo.MongoClient', return_value=client,
        ) as factory, patch(
            'src.infrastructure.object_storage.minio_store.object_store_from_settings', return_value='store',
        ), patch(
            'src.infrastructure.object_storage.catalog.import_local_catalog', return_value=preview,
        ) as importer:
            result = CliRunner().invoke(cli, [str(self.root)])
        self.assertEqual(result.exit_code, 0, result.output)
        database.__getitem__.assert_called_once_with('catalog_v2')
        importer.assert_called_once_with(str(self.root), 'store', 'selected collection', None)
        factory.assert_called_once_with(
            settings.mongodb_uri, tz_aware=True,
            connectTimeoutMS=changed.mongo.connect_timeout_ms,
            socketTimeoutMS=changed.mongo.socket_timeout_ms,
            serverSelectionTimeoutMS=changed.mongo.server_selection_timeout_ms,
            waitQueueTimeoutMS=changed.mongo.wait_queue_timeout_ms,
        )
