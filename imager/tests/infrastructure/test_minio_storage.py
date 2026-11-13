import hashlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import cv2
import numpy as np
from imager_config.loader import load_config
from imager_contracts.protocol import JPEG_CONTENT_TYPE, SEED_UPPER_BOUND
from src.application.image_builder.services.object_render import ObjectRenderExecutor, RenderRejected
from src.infrastructure.object_storage.catalog import (
    MinioCatalogProvider, import_local_catalog, preview_local_catalog,
)
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

    def remove_object(self, bucket, key):
        self.objects.pop((bucket, key), None)


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

    def test_retention_delete_verifies_reference_and_is_idempotent(self):
        reference = self.store.upload_file(self.write(), 'uploads/job/source.bin')
        self.store.delete_file(reference)
        self.store.delete_file(reference)
        self.assertNotIn(('imager', reference['key']), self.client.objects)

    def test_retention_delete_refuses_replaced_object(self):
        reference = self.store.upload_file(self.write(), 'uploads/job/source.bin')
        self.client.objects['imager', reference['key']] = (b'replaced', {'x-amz-meta-sha256': 'a' * 64})
        with self.assertRaises(ObjectIntegrityError):
            self.store.delete_file(reference)
        self.assertIn(('imager', reference['key']), self.client.objects)

    def test_retention_delete_propagates_transport_failure(self):
        reference = self.store.upload_file(self.write(), 'uploads/job/source.bin')
        with patch.object(self.client, 'remove_object', side_effect=OSError('temporary network failure')):
            with self.assertRaises(ObjectStoreError):
                self.store.delete_file(reference)
        self.assertIn(('imager', reference['key']), self.client.objects)

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

    def test_catalog_preview_is_offline_and_matches_unique_import_capacity(self):
        group = self.root / 'cats'
        group.mkdir()
        path = group / 'one.png'
        cv2.imwrite(str(path), np.full((20, 30, 3), (30, 90, 180), dtype=np.uint8))
        original = path.read_bytes()
        (group / 'duplicate.png').write_bytes(original)
        preview = preview_local_catalog(self.root)
        self.assertEqual(self.client.upload_count, 0)
        self.assertEqual(self.client.get_count, 0)
        self.assertEqual(preview[0]['source_files'], 2)
        self.assertEqual(preview[0]['count'], 1)
        self.assertEqual(preview[0]['duplicate_files'], 1)
        self.assertEqual(preview[0]['source_bytes'], len(original) * 2)
        self.assertEqual(preview[0]['unique_image_bytes'], len(original))
        self.assertEqual(preview[0]['normalized_cache_bytes'], 20 * 30 * 3)
        self.assertEqual((preview[0]['width_min'], preview[0]['height_max']), (30, 20))
        imported = import_local_catalog(self.root, self.store, Groups())
        self.assertEqual(preview[0]['revision'], imported[0]['revision'])
        self.assertEqual(preview[0]['manifest'], imported[0]['manifest'])
        self.assertEqual(path.read_bytes(), original)

    def test_bad_local_image_is_rejected_before_bucket_or_registry_mutation(self):
        group = self.root / 'cats'
        group.mkdir()
        (group / 'broken.png').write_bytes(b'not an encoded image')
        groups = Groups()
        with patch.object(self.store, 'ensure_bucket') as ensure_bucket:
            with self.assertRaises(ValueError):
                import_local_catalog(self.root, self.store, groups)
        ensure_bucket.assert_not_called()
        self.assertEqual(groups.documents, {})
        self.assertEqual(self.client.upload_count, 0)

    def test_empty_catalog_root_does_not_report_a_successful_import(self):
        with self.assertRaises(ValueError):
            preview_local_catalog(self.root)

    def test_manifest_order_is_identical_for_windows_and_linux_filename_case(self):
        group = self.root / 'cats'
        group.mkdir()
        for name, color in (('a.png', (5, 10, 15)), ('Z.png', (50, 60, 70))):
            cv2.imwrite(str(group / name), np.full((20, 20, 3), color, dtype=np.uint8))
        report = import_local_catalog(self.root, self.store, Groups())
        manifest = self.store.read_json(report[0]['manifest'])
        self.assertEqual([image['color'] for image in manifest['images']],
                         [[50, 60, 70], [5, 10, 15]])

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

    def test_configured_result_filename_and_key_keep_jpeg_encoding_consistent(self):
        group = self.prepare_catalog()
        source = self.root / 'source.png'
        cv2.imwrite(str(source), np.full((24, 32, 3), 100, dtype=np.uint8))
        reference = self.store.upload_file(source, 'uploads/job/source.png')
        request = dict(job_id='job', source=reference, catalog_manifest=group['manifest'], seed=5,
                       parameters=dict(group_name='cats', insertion_format='crop', alpha_channel=0,
                                       noise_level=0, result_size=4, cell_size=3))
        override = self.root / 'override.toml'
        override.write_text(
            '[storage]\nresult_filename="completed.jpeg"\n'
            'result_key_template="renders/{job_id}/{execution_token}/{result_filename}"\n'
            'source_filename="input.data"\n[render]\njpeg_quality=84\n',
            encoding='utf-8',
        )
        with patch('src.application.image_builder.services.object_render.config', load_config(override)):
            executor = ObjectRenderExecutor(self.store, MinioCatalogProvider(self.store))
            published = executor.execute(request, 'attempt')
        self.assertEqual(executor.jpeg_quality, 84)
        self.assertEqual(published['key'], 'renders/job/attempt/completed.jpeg')
        self.assertEqual(published['content_type'], JPEG_CONTENT_TYPE)
        encoded = self.client.objects[published['bucket'], published['key']][0]
        self.assertTrue(encoded.startswith(b'\xff\xd8'))
        self.assertEqual(cv2.imdecode(np.frombuffer(encoded, dtype=np.uint8), cv2.IMREAD_COLOR).shape,
                         (9, 12, 3))

    def test_changed_catalog_templates_preserve_old_pinned_manifest_references(self):
        old = self.prepare_catalog()
        override = self.root / 'catalog-override.toml'
        override.write_text(
            '[storage]\ncatalog_image_key_template="assets/{sha256}{extension}"\n'
            'catalog_manifest_key_template="indexes/{group_id}/{sha256}.json"\n',
            encoding='utf-8',
        )
        changed = load_config(override)
        with patch('src.infrastructure.object_storage.catalog.config', changed):
            provider = MinioCatalogProvider(self.store)
            catalog = provider.for_manifest(old['manifest'], 'cats')
            selected = catalog.find_closest_metadata([(30, 90, 180)], 'cats').value[0]
            self.assertTrue(catalog.get_cell_image(selected))
            new = import_local_catalog(
                self.root / 'cats', self.store, Groups(), group_name='new-cats',
            )[0]
            self.assertTrue(new['manifest']['key'].startswith('indexes/'))
            catalog = provider.for_manifest(new['manifest'], 'new-cats')
            selected = catalog.find_closest_metadata([(30, 90, 180)], 'new-cats').value[0]
            self.assertTrue(selected.object_ref['key'].startswith('assets/'))
            self.assertTrue(catalog.get_cell_image(selected))
        # Existing manifests remain readable even after another process starts
        # with a different current storage layout.
        catalog = MinioCatalogProvider(self.store).for_manifest(new['manifest'], 'new-cats')
        selected = catalog.find_closest_metadata([(30, 90, 180)], 'new-cats').value[0]
        self.assertTrue(catalog.get_cell_image(selected))

    def test_seed_policy_matches_wire_contract_before_infrastructure_calls(self):
        executor = ObjectRenderExecutor(self.store, MinioCatalogProvider(self.store))
        parameters = dict(group_name='cats', insertion_format='crop', alpha_channel=0,
                          noise_level=0, result_size=4, cell_size=3)
        for seed in (-1, True, SEED_UPPER_BOUND):
            with self.subTest(seed=seed), self.assertRaises(RenderRejected):
                executor.execute({'job_id': 'job', 'parameters': parameters, 'seed': seed}, 'token')
        self.assertEqual(self.client.get_count, 0)
