import hashlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np
from src.application.image_builder.services.object_render import ObjectRenderExecutor, RenderRejected
from src.infrastructure.object_storage.catalog import MinioCatalogProvider, import_local_catalog
from src.infrastructure.object_storage.minio_store import (
    MinioObjectStore, ObjectIntegrityError, ObjectStoreError,
)


class MissingObject(Exception):
    code = 'NoSuchKey'


class Response(io.BytesIO):
    def __init__(self, data):
        super().__init__(data)
        self.released = False

    def release_conn(self):
        self.released = True


class FakeMinio:
    def __init__(self):
        self.objects = {}
        self.get_count = 0
        self.responses = []
        self.upload_count = 0

    def bucket_exists(self, bucket):
        return True

    def stat_object(self, bucket, key):
        try:
            data, metadata = self.objects[bucket, key]
        except KeyError:
            raise MissingObject()
        return SimpleNamespace(size=len(data), metadata=metadata)

    def fput_object(self, bucket, key, path, content_type, metadata, num_parallel_uploads):
        assert num_parallel_uploads == 1
        self.objects[bucket, key] = (
            Path(path).read_bytes(), {'x-amz-meta-sha256': metadata['sha256']},
        )
        self.upload_count += 1

    def get_object(self, bucket, key):
        self.get_count += 1
        try:
            data = self.objects[bucket, key][0]
        except KeyError:
            raise MissingObject()
        response = Response(data)
        self.responses.append(response)
        return response


class Groups:
    def __init__(self):
        self.documents = {}

    def update_one(self, query, changes, upsert=False):
        self.documents[query['_id']] = {'_id': query['_id'], **changes['$set']}


class TestMinioStorage(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.client = FakeMinio()
        self.store = MinioObjectStore('unused:9000', 'key', 'secret', 'imager', client=self.client)

    def write(self, name='source.bin', data=b'image-bytes'):
        path = self.root / name
        path.write_bytes(data)
        return path

    def test_roundtrip_stream_is_closed_and_released(self):
        reference = self.store.upload_file(self.write(), 'uploads/job/source.bin')
        downloaded = self.store.download_file(reference, self.root / 'downloaded.bin')
        self.assertEqual(Path(downloaded).read_bytes(), b'image-bytes')
        self.assertTrue(self.client.responses[0].closed)
        self.assertTrue(self.client.responses[0].released)

    def test_same_object_is_idempotent_but_different_bytes_are_rejected(self):
        source = self.write()
        first = self.store.upload_file(source, 'uploads/job/source.bin')
        self.assertEqual(first, self.store.upload_file(source, 'uploads/job/source.bin'))
        self.assertEqual(self.client.upload_count, 1)
        source.write_bytes(b'different')
        with self.assertRaises(ObjectIntegrityError):
            self.store.upload_file(source, 'uploads/job/source.bin')

    def test_hash_and_size_failures_never_replace_destination(self):
        ref = self.store.upload_file(self.write(), 'uploads/job/source.bin')
        destination = self.write('destination.bin', b'previous')
        for invalid in ({**ref, 'sha256': '0' * 64}, {**ref, 'size_bytes': 2}):
            with self.subTest(invalid=invalid), self.assertRaises(ObjectIntegrityError):
                self.store.download_file(invalid, destination)
            self.assertEqual(destination.read_bytes(), b'previous')
            self.assertFalse(list(self.root.glob('.download-*')))
        self.assertTrue(all(response.closed and response.released for response in self.client.responses))

    def test_invalid_reference_rejected_before_network(self):
        valid = dict(bucket='imager', key='uploads/source', sha256='a' * 64, size_bytes=5)
        for invalid in (
            {**valid, 'bucket': 'another'}, {**valid, 'key': '../outside'},
            {**valid, 'key': '/absolute'}, {**valid, 'size_bytes': 100},
            {**valid, 'sha256': 'not-a-digest'},
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.store.download_file(invalid, self.root / 'out.bin', max_bytes=10)
        self.assertEqual(self.client.get_count, 0)

    def test_missing_object_is_permanent_but_connection_error_is_retryable(self):
        reference = dict(bucket='imager', key='uploads/missing', sha256='a' * 64, size_bytes=1)
        with self.assertRaises(ObjectStoreError) as failed:
            self.store.download_file(reference, self.root / 'out.bin')
        self.assertFalse(failed.exception.retryable)
        self.client.get_object = lambda *args: (_ for _ in ()).throw(ConnectionError('offline'))
        with self.assertRaises(ObjectStoreError) as failed:
            self.store.download_file(reference, self.root / 'out.bin')
        self.assertTrue(failed.exception.retryable)

    def test_catalog_import_is_explicit_idempotent_and_does_not_modify_local_files(self):
        group = self.root / 'cats'
        group.mkdir()
        path = group / 'one.png'
        cv2.imwrite(str(path), np.full((20, 30, 3), (30, 90, 180), dtype=np.uint8))
        original = path.read_bytes()
        groups = Groups()
        report = import_local_catalog(self.root, self.store, groups)
        self.assertEqual(report[0]['count'], 1)
        self.assertEqual(path.read_bytes(), original)
        uploads = self.client.upload_count
        second = import_local_catalog(self.root, self.store, groups)
        self.assertEqual(second[0]['revision'], report[0]['revision'])
        self.assertEqual(self.client.upload_count, uploads)

    def prepare_catalog(self):
        group = self.root / 'cats'
        group.mkdir()
        path = group / 'one.png'
        cv2.imwrite(str(path), np.full((20, 30, 3), (30, 90, 180), dtype=np.uint8))
        groups = Groups()
        return import_local_catalog(self.root, self.store, groups)[0]

    def test_catalog_loads_only_manifest_then_caches_original_lazily(self):
        group = self.prepare_catalog()
        provider = MinioCatalogProvider(self.store)
        catalog = provider.for_manifest(group['manifest'], 'cats')
        self.assertEqual(self.client.get_count, 1)
        matched = catalog.find_closest_metadata([(30, 90, 180)] * 2, 'cats')
        first = catalog.get_cell_image(matched.value[0])
        second = catalog.get_cell_image(matched.value[1])
        self.assertTrue(first)
        self.assertIs(first.value, second.value)
        self.assertEqual(self.client.get_count, 2)
        self.assertIs(catalog, provider.for_manifest(group['manifest'], 'cats'))
        self.assertEqual(catalog.cache_bytes, 20 * 30 * 3)

    def test_render_has_no_shared_local_path_and_publishes_attempt_object(self):
        group = self.prepare_catalog()
        source = self.root / 'source.png'
        cv2.imwrite(str(source), np.full((24, 32, 3), 100, dtype=np.uint8))
        source_ref = self.store.upload_file(source, 'uploads/job1/source.png', 'image/png')
        request = dict(job_id='job1', source=source_ref, catalog_manifest=group['manifest'], seed=5,
                       parameters=dict(group_name='cats', insertion_format='crop', alpha_channel=0,
                                       noise_level=0, result_size=4, cell_size=3))
        stages = []
        executor = ObjectRenderExecutor(self.store, MinioCatalogProvider(self.store))
        first = executor.execute(request, 'token1', stages.append)
        second = executor.execute(request, 'token2')
        self.assertEqual(first['key'], 'jobs/job1/attempts/token1/result.jpg')
        self.assertNotEqual(first['key'], second['key'])
        self.assertEqual(first['sha256'], second['sha256'])
        encoded = self.client.objects[first['bucket'], first['key']][0]
        image = cv2.imdecode(np.frombuffer(encoded, dtype=np.uint8), cv2.IMREAD_COLOR)
        self.assertEqual(image.shape, (9, 12, 3))
        self.assertIn('rendering', [stage['stage'] for stage in stages])

    def test_rejected_parameters_do_not_touch_object_storage(self):
        executor = ObjectRenderExecutor(self.store, MinioCatalogProvider(self.store))
        with self.assertRaises(RenderRejected):
            executor.execute({'job_id': 'job', 'parameters': {}}, 'token')
        self.assertEqual(self.client.get_count, 0)
