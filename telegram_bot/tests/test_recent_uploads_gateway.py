"""Offline core-gallery boundary tests, without Mongo, Telegram or MinIO I/O."""
import copy
import hashlib
import io
import tempfile
import threading
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from imager_config.loader import config
from src.infrastructure.services.imager_service.imager_service import ImagerService
from src.infrastructure.services.object_storage import BotObjectStorage


_MISSING = object()


def value(document, path):
    for part in path.split('.'):
        if not isinstance(document, dict) or part not in document:
            return _MISSING
        document = document[part]
    return document


def reference(job_id='job-1', data=b'photo', **changes):
    digest = hashlib.sha256(data).hexdigest()
    result = {
        'bucket': 'imager',
        'key': config.storage.source_key_template.format(job_id=job_id, sha256=digest),
        'sha256': digest, 'size_bytes': len(data), 'content_type': 'image/jpeg',
    }
    result.update(changes)
    return result


def job(job_id='job-1', *, client='telegram', state=None, created_at=None, source=None):
    result = {
        '_id': job_id,
        'created_at': created_at or datetime(2026, 11, 23, tzinfo=timezone.utc),
        'request': {
            'client_id': client, 'source': reference(job_id) if source is None else source,
            'client_context': {'user_id': 7, 'chat_id': 7},
            'parameters': {'group_name': 'cats'},
        },
        'private_internal_field': 'must not reach the gallery',
    }
    if state is not None:
        result['artifact_lifecycle'] = {'source': {'state': state}}
    return result


class Cursor:
    def __init__(self, rows, collection):
        self.rows, self.collection = rows, collection

    def sort(self, ordering):
        self.collection.ordering = ordering
        for key, direction in reversed(ordering):
            self.rows.sort(key=lambda row: value(row, key), reverse=direction < 0)
        return self

    def limit(self, count):
        if not 0 < count <= 10:
            raise AssertionError('A Mongo gallery cursor must remain bounded')
        self.collection.limit = count
        self.rows = self.rows[:count]
        return self

    def __iter__(self):
        return iter(self.rows)


class Jobs:
    def __init__(self, rows):
        self.rows = copy.deepcopy(rows)
        self.find_calls = []
        self.find_one_calls = []
        self.thread_ids = []

    def find(self, query, projection):
        self.find_calls.append((copy.deepcopy(query), copy.deepcopy(projection)))
        self.thread_ids.append(threading.get_ident())
        matching = []
        for row in self.rows:
            for field, expected in query.items():
                actual = value(row, field)
                if isinstance(expected, dict):
                    if '$exists' in expected and (actual is not _MISSING) != expected['$exists']:
                        break
                    if '$ne' in expected and actual == expected['$ne']:
                        break
                    if '$nin' in expected and actual in expected['$nin']:
                        break
                elif actual != expected:
                    break
            else:
                projected = {}
                for field in projection:
                    selected = value(row, field)
                    if selected is _MISSING:
                        continue
                    target = projected
                    parts = field.split('.')
                    for part in parts[:-1]:
                        target = target.setdefault(part, {})
                    target[parts[-1]] = copy.deepcopy(selected)
                matching.append(projected)
        return Cursor(matching, self)

    def find_one(self, query):
        self.find_one_calls.append(copy.deepcopy(query))
        self.thread_ids.append(threading.get_ident())
        return next((copy.deepcopy(row) for row in self.rows
                     if row['_id'] == query['_id']), None)


def gateway(rows, store=None):
    collection = Jobs(rows)
    store = store or SimpleNamespace(bucket='imager', download_result=Mock())
    service = ImagerService(
        SimpleNamespace(), database={config.mongo.jobs_collection: collection},
        object_store=store,
    )
    return service, collection, store


class RecentUploadsGatewayTests(unittest.IsolatedAsyncioTestCase):
    async def test_recent_sources_are_telegram_only_available_and_ordered(self):
        now = datetime(2026, 11, 23, tzinfo=timezone.utc)
        missing = job('missing')
        missing['request'].pop('source')
        empty = job('empty')
        empty['request']['source'] = None
        rows = [job('older', created_at=now - timedelta(days=1)),
                job('tie-a', created_at=now), job('tie-z', created_at=now),
                job('pending', state='pending', created_at=now - timedelta(hours=1)),
                job('foreign', client='http'), missing, empty]
        rows += [job(state, state=state) for state in ('deleted', 'deleting', 'unmanaged')]
        service, collection, _ = gateway(rows)
        result = await service.recent_uploads()
        self.assertEqual([row['_id'] for row in result], ['tie-z', 'tie-a', 'pending', 'older'])
        self.assertNotIn('private_internal_field', result[0])
        self.assertEqual(result[0]['request']['parameters'], {'group_name': 'cats'})
        self.assertEqual(result[0]['request']['client_context']['user_id'], 7)
        self.assertEqual(collection.ordering, [('created_at', -1), ('_id', -1)])
        self.assertEqual(collection.find_calls[0][1], {
            '_id': 1, 'created_at': 1, 'request.source': 1,
            'request.client_context': 1, 'request.parameters': 1,
            'artifact_lifecycle.source': 1,
        })
        self.assertNotEqual(collection.thread_ids[0], threading.get_ident())

    async def test_recent_upload_limit_is_capped_and_zero_never_unbounds_mongo(self):
        service, collection, _ = gateway([job(f'job-{number:02d}') for number in range(20)])
        self.assertEqual(len(await service.recent_uploads(1000)), 10)
        self.assertEqual(collection.limit, 10)
        self.assertEqual(len(await service.recent_uploads(3)), 3)
        collection.find_calls.clear()
        for limit in (0, -1):
            self.assertEqual(await service.recent_uploads(limit), [])
        self.assertEqual(collection.find_calls, [])
        for limit in (True, '10', 1.5, None):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                await service.recent_uploads(limit)

    async def test_download_reloads_job_and_uses_bounded_background_transfer(self):
        service, collection, store = gateway([job()])
        await service.recent_uploads()
        changed = reference(data=b'new-photo')
        collection.rows[0]['request']['source'] = changed
        transfer_threads = []
        store.download_result.side_effect = lambda *args, **kwargs: transfer_threads.append(threading.get_ident())
        destination = Path('download.jpg')
        self.assertEqual(await service.download_source('job-1', destination), str(destination))
        self.assertEqual(collection.find_one_calls, [{'_id': 'job-1'}])
        store.download_result.assert_called_once_with(
            changed, destination, max_bytes=config.limits.source_file_bytes,
        )
        self.assertNotEqual(transfer_threads[0], threading.get_ident())
        self.assertNotEqual(collection.thread_ids[-1], threading.get_ident())

    async def test_retention_change_after_listing_prevents_download(self):
        for state in ('deleted', 'deleting', 'unmanaged'):
            with self.subTest(state=state):
                service, collection, store = gateway([job()])
                self.assertEqual(len(await service.recent_uploads()), 1)
                collection.rows[0]['artifact_lifecycle'] = {'source': {'state': state}}
                with self.assertRaises(ValueError):
                    await service.download_source('job-1', 'unused.jpg')
                store.download_result.assert_not_called()

    async def test_missing_foreign_and_inconsistent_jobs_are_rejected(self):
        for rows in ([], [job(client='api')], [job(source={})]):
            with self.subTest(rows=rows):
                service, _, store = gateway(rows)
                with self.assertRaises(ValueError):
                    await service.download_source('job-1', 'unused.jpg')
                store.download_result.assert_not_called()
        service, collection, store = gateway([job()])
        collection.find_one = Mock(return_value=job('other-job'))
        with self.assertRaises(ValueError):
            await service.download_source('job-1', 'unused.jpg')
        store.download_result.assert_not_called()

    async def test_invalid_job_ids_do_not_reach_mongo(self):
        service, collection, store = gateway([job()])
        for identifier in ({'$ne': None}, None, '', '../job-1', 'a' * 129):
            with self.subTest(identifier=identifier), self.assertRaises(ValueError):
                await service.download_source(identifier, 'unused.jpg')
        self.assertEqual(collection.find_one_calls, [])
        store.download_result.assert_not_called()

    async def test_unowned_bucket_key_and_malformed_references_are_rejected(self):
        refs = [reference(bucket='other-bucket'), reference(job_id='other-job'),
                reference(key='catalog/images/photo.jpg'),
                reference(key='uploads/job-1/../photo.jpg'), reference(sha256='invalid'),
                reference(size_bytes=0), reference(size_bytes=config.limits.source_file_bytes + 1)]
        for source in refs:
            with self.subTest(source=source):
                service, _, store = gateway([job(source=source)])
                with self.assertRaises(ValueError):
                    await service.download_source('job-1', 'unused.jpg')
                store.download_result.assert_not_called()

    async def test_custom_source_layout_and_legacy_filenames_remain_readable(self):
        configured = replace(config, storage=replace(
            config.storage, source_key_template='new-uploads/{sha256}/{job_id}.bin',
            source_filename='legacy-source.bin', upload_filename='legacy-upload.jpg',
        ))
        digest = reference()['sha256']
        keys = [f'new-uploads/{digest}/job-1.bin',
                'uploads/job-1/legacy-source.bin', 'uploads/job-1/legacy-upload.jpg']
        with patch('src.infrastructure.services.imager_service.imager_service.runtime_config', configured):
            for key in keys:
                with self.subTest(key=key):
                    service, _, store = gateway([job(source=reference(key=key))])
                    self.assertEqual(await service.download_source('job-1', 'source.bin'), 'source.bin')
                    store.download_result.assert_called_once()

    async def test_source_template_cannot_override_protected_catalog_import_prefixes(self):
        protected = ['catalog/'] + [template.split('{', 1)[0] for template in (
            config.storage.catalog_image_key_template,
            config.storage.catalog_manifest_key_template,
            config.pack_import.archive_key_template,
        )]
        for prefix in protected:
            with self.subTest(prefix=prefix):
                template = prefix + '{job_id}/{sha256}.jpg'
                configured = replace(config, storage=replace(config.storage, source_key_template=template))
                source = reference(key=template.format(job_id='job-1', sha256=reference()['sha256']))
                service, _, store = gateway([job(source=source)])
                with patch('src.infrastructure.services.imager_service.imager_service.runtime_config', configured):
                    with self.assertRaises(ValueError):
                        await service.download_source('job-1', 'unused.jpg')
                store.download_result.assert_not_called()

    async def test_missing_and_pending_lifecycle_are_readable_but_malformed_is_not(self):
        for lifecycle in (None, {}, {'source': {'state': 'pending'}},
                          {'source': 'invalid'}, 'invalid'):
            service, collection, store = gateway([job()])
            if lifecycle is not None:
                collection.rows[0]['artifact_lifecycle'] = lifecycle
            with self.subTest(lifecycle=lifecycle):
                if lifecycle in ({'source': 'invalid'}, 'invalid'):
                    with self.assertRaises(ValueError):
                        await service.download_source('job-1', 'unused.jpg')
                    store.download_result.assert_not_called()
                else:
                    await service.download_source('job-1', 'source.jpg')
                    store.download_result.assert_called_once()

    async def test_checksum_failure_propagates_without_replacing_destination(self):
        class Response(io.BytesIO):
            def release_conn(self):
                pass

        source = reference(data=b'valid')
        minio = SimpleNamespace(
            stat_object=Mock(return_value=SimpleNamespace(size=source['size_bytes'])),
            get_object=Mock(side_effect=lambda *args: Response(b'wrong')),
        )
        store = BotObjectStorage('', '', '', 'imager', client=minio)
        service, _, _ = gateway([job(source=source)], store)
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / 'source.jpg'
            destination.write_bytes(b'previous')
            with self.assertRaisesRegex(ValueError, 'checksum'):
                await service.download_source('job-1', destination)
            self.assertEqual(destination.read_bytes(), b'previous')
            self.assertEqual(list(Path(directory).iterdir()), [destination])

