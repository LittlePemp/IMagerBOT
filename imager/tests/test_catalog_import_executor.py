import hashlib
import io
import json
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import cv2
import numpy as np

from imager_config.loader import config
from imager_contracts import ObjectRef
from imager_contracts.catalog_import import CatalogImportRequest
from src.application.catalog_import.errors import InvalidCatalogArchive
from src.application.catalog_import.executor import CatalogImportExecutor
from src.infrastructure.catalog_import.mongo_imports import MongoImportRepository
from test_catalog_import_repository import Collection


def png(color):
    valid, encoded = cv2.imencode('.png', np.full((24, 24, 3), color, dtype=np.uint8))
    assert valid
    return encoded.tobytes()


def archive_bytes(members):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w') as archive:
        for name, data in members:
            archive.writestr(name, data)
    return stream.getvalue()


class Store:
    bucket = 'imager'

    def __init__(self, archive):
        self.archive = archive
        self.objects = {}
        self.fail_upload = None
        self.download_max = None

    def ensure_bucket(self):
        pass

    def validate_reference(self, reference, maximum):
        if not 0 < reference['size_bytes'] <= maximum:
            raise ValueError('invalid size')
        return reference

    def download_file(self, reference, path, *, max_bytes):
        self.download_max = max_bytes
        if hashlib.sha256(self.archive).hexdigest() != reference['sha256']:
            raise ValueError('invalid digest')
        Path(path).write_bytes(self.archive)
        return str(path)

    def upload_file(self, path, key, content_type):
        if self.fail_upload is not None:
            self.fail_upload -= 1
            if self.fail_upload == 0:
                self.fail_upload = None
                raise OSError('temporary upload failure')
        data = Path(path).read_bytes()
        if key in self.objects and self.objects[key] != data:
            raise ValueError('immutable key changed')
        self.objects[key] = data
        return ObjectRef(self.bucket, key, hashlib.sha256(data).hexdigest(), len(data), content_type).to_dict()

    def put_json(self, value, key):
        data = json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()
        self.objects[key] = data
        return ObjectRef(self.bucket, key, hashlib.sha256(data).hexdigest(), len(data), 'application/json').to_dict()


class TestCatalogImportExecutor(unittest.TestCase):
    def setUp(self):
        # Positive importer behavior must not depend on the test runner's /tmp
        # being a deliberately small tmpfs. Capacity rejection is tested below.
        space = patch('src.application.catalog_import.executor.shutil.disk_usage',
                      return_value=SimpleNamespace(free=10 ** 12))
        space.start()
        self.addCleanup(space.stop)

    def setup_import(self, members):
        blob = archive_bytes(members)
        self.request = CatalogImportRequest(
            'import_1', 123, 'pack_test', 'Пак с иконками',
            ObjectRef('imager', 'imports/source.zip', hashlib.sha256(blob).hexdigest(), len(blob), 'application/zip'),
        ).to_dict()
        self.jobs, self.catalogs = Collection(), Collection()
        self.repository = MongoImportRepository(self.jobs, self.catalogs)
        self.repository.receive(self.request)
        self.store = Store(blob)
        self.executor = CatalogImportExecutor(self.store, self.repository)
        self.progress = []

    def execute(self, claim):
        def progress(details):
            self.repository.heartbeat_execution('import_1', claim['token'], details)
            self.progress.append(details)
        return self.executor.execute(self.request, claim['token'], progress)

    def test_valid_nested_pack_imports_originals_and_atomic_manifest_and_deduplicates(self):
        red, blue = png((0, 0, 255)), png((255, 0, 0))
        self.setup_import([('pack/red.png', red), ('pack/nested/blue.png', blue), ('duplicate.png', red)])
        claim = self.repository.claim_execution(self.request)
        result = self.execute(claim)
        self.repository.complete('import_1', claim['token'], result)
        self.assertEqual(result['count'], 2)
        self.assertEqual(result['display_name'], 'Пак с иконками')
        self.assertEqual(len(self.store.objects), 3)
        self.assertIn(red, self.store.objects.values())
        self.assertIn(blue, self.store.objects.values())
        self.assertEqual(self.catalogs.documents['pack_test']['publication_state'], 'completed')
        self.assertEqual(self.repository.get('import_1')['status'], 'completed')
        self.assertEqual(self.store.download_max, config.pack_import.max_archive_bytes)
        self.assertTrue({'extracting', 'validating', 'uploading', 'publishing'} <= {item['phase'] for item in self.progress})

    def test_low_disk_rejects_before_downloading_or_publishing(self):
        self.setup_import([('valid.png', png((1, 2, 3)))])
        claim = self.repository.claim_execution(self.request)
        with patch('src.application.catalog_import.executor.shutil.disk_usage',
                   return_value=SimpleNamespace(free=1)), self.assertRaises(InvalidCatalogArchive):
            self.execute(claim)
        self.assertIsNone(self.store.download_max)
        self.assertEqual(self.store.objects, {})
        self.assertEqual(self.catalogs.documents['pack_test']['count'], 0)
        self.assertTrue(all('path' not in item for item in self.progress))

    def test_invalid_later_image_prevents_all_image_uploads_and_publication(self):
        self.setup_import([('valid.png', png((1, 2, 3))), ('broken.png', b'not an image')])
        claim = self.repository.claim_execution(self.request)
        with self.assertRaises(InvalidCatalogArchive) as error:
            self.execute(claim)
        self.repository.fail('import_1', claim['token'], error.exception)
        self.assertEqual(self.store.objects, {})
        self.assertEqual(self.catalogs.documents['pack_test']['count'], 0)
        self.assertEqual(self.repository.get('import_1')['status'], 'failed')
        self.assertIn('изображение', self.repository.get('import_1')['public_error'])

    def test_upload_failure_keeps_catalog_unpublished_and_retry_reuses_immutable_objects(self):
        self.setup_import([('red.png', png((0, 0, 255))), ('blue.png', png((255, 0, 0)))])
        first = self.repository.claim_execution(self.request)
        self.store.fail_upload = 2
        with self.assertRaises(OSError):
            self.execute(first)
        self.assertEqual(self.catalogs.documents['pack_test']['count'], 0)
        self.assertEqual(len(self.store.objects), 1)
        self.repository.retry_execution('import_1', first['token'], OSError('retry'))
        second = self.repository.claim_execution(self.request)
        result = self.execute(second)
        self.repository.complete('import_1', second['token'], result)
        self.assertEqual(result['count'], 2)
        self.assertEqual(len(self.store.objects), 3)
        self.assertTrue(self.repository.claim_execution(self.request)['already_completed'])
