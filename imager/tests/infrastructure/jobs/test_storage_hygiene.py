import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

from src.infrastructure.jobs.storage_hygiene import StorageHygiene, archive_prefix, result_job_id


class Cursor(list):
    def sort(self, *args):
        return self

    def limit(self, count):
        return Cursor(self[:count])


class TestKeyLayouts(unittest.TestCase):
    def test_result_job_identity(self):
        self.assertEqual(result_job_id('jobs/job_1/attempts/abc123/result.jpg'), 'job_1')
        for key in ('jobs/job_1/attempts/abc/other.jpg', 'catalog/images/x.png',
                    'jobs/../attempts/abc/result.jpg', 'uploads/job_1/source.jpg', ''):
            with self.subTest(key=key):
                self.assertIsNone(result_job_id(key))

    def test_archive_prefix_is_scoped_to_one_import(self):
        self.assertEqual(archive_prefix('imp_1'), 'catalog/imports/imp_1/')


class TestStorageHygiene(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 24, tzinfo=timezone.utc)
        self.jobs, self.imports = Mock(), Mock()
        self.store = Mock(bucket='imager')
        self.hygiene = StorageHygiene(self.jobs, self.imports, self.store, clock=lambda: self.now)

    def test_archive_outside_import_layout_is_never_deleted(self):
        self.imports.find.return_value = Cursor([{
            '_id': 'imp_1', 'request': {'source': {'bucket': 'imager', 'key': 'catalog/images/a.png'}},
        }])
        self.imports.update_one.return_value = SimpleNamespace(matched_count=1)
        self.assertEqual(self.hygiene.cleanup_import_archives(), 0)
        self.store.delete_file.assert_not_called()
        update = self.imports.update_one.call_args.args[1]['$set']
        self.assertEqual(update['archive_lifecycle.state'], 'unmanaged')

    def test_failed_archive_deletion_is_retried_later(self):
        source = {'bucket': 'imager', 'key': 'catalog/imports/imp_1/abc.zip'}
        self.imports.find.return_value = Cursor([{'_id': 'imp_1', 'request': {'source': source}}])
        self.imports.update_one.return_value = SimpleNamespace(matched_count=1)
        self.store.delete_file.side_effect = OSError('minio down')
        self.assertEqual(self.hygiene.cleanup_import_archives(), 0)
        update = self.imports.update_one.call_args.args[1]['$set']
        self.assertEqual(update['archive_lifecycle.state'], 'pending')

    def test_live_attempt_and_canonical_results_are_kept(self):
        old = self.now - timedelta(days=1)
        self.store.list_objects.return_value = [
            ('jobs/live/attempts/t1/result.jpg', old),
            ('jobs/done/attempts/t2/result.jpg', old),
            ('jobs/done/attempts/t0/result.jpg', old),
            ('jobs/done/attempts/t3/result.jpg', self.now),
        ]
        documents = {'live': {'status': 'running'},
                     'done': {'status': 'completed', 'result': {'key': 'jobs/done/attempts/t2/result.jpg'}}}
        self.jobs.find_one.side_effect = lambda query, projection: documents.get(query['_id'])
        self.store.reference_for_key.side_effect = lambda key: {'key': key}
        self.assertEqual(self.hygiene.cleanup_orphan_results(), 1)
        self.store.delete_file.assert_called_once_with({'key': 'jobs/done/attempts/t0/result.jpg'})

    def test_document_purge_requires_terminal_state_and_released_artifacts(self):
        self.jobs.delete_many.return_value = SimpleNamespace(deleted_count=3)
        self.assertEqual(self.hygiene.purge_job_documents(), 3)
        query = self.jobs.delete_many.call_args.args[0]
        self.assertEqual(query['status'], {'$in': ['completed', 'failed']})
        self.assertIn('$lt', query['updated_at'])
        self.assertEqual(query['completion_outbox.state'], {'$nin': ['pending', 'preparing', 'claimed']})
        self.assertEqual(len(query['$and']), 2)

    def test_one_failing_step_does_not_block_the_rest(self):
        self.imports.find.side_effect = OSError('mongo down')
        self.store.list_objects.return_value = []
        self.jobs.delete_many.return_value = SimpleNamespace(deleted_count=0)
        report = self.hygiene.cleanup_once()
        self.assertEqual(report, {'orphan_results': 0, 'job_documents': 0})


class TestLostAttemptResultCleanup(unittest.TestCase):
    def test_lost_lease_removes_the_unpublished_result_only(self):
        from unittest.mock import patch
        from src.infrastructure.jobs.mongo_jobs import LostExecution
        from src.presentation.temporal.activities import RenderActivities
        reference = {'bucket': 'imager', 'key': 'jobs/job_1/attempts/token/result.jpg',
                     'sha256': 'c' * 64, 'size_bytes': 10, 'content_type': 'image/jpeg'}
        repository = Mock()
        repository.claim_execution.return_value = {'already_completed': False, 'token': 'token', 'fence': 1}
        repository.complete.side_effect = LostExecution('stale')
        repository.collection.find_one.return_value = {'result': None}
        executor = Mock()
        executor.execute.return_value = reference
        activities = RenderActivities(repository, executor)
        with patch('src.presentation.temporal.activities.activity.heartbeat'), \
                patch('src.presentation.temporal.activities.activity.is_cancelled', return_value=False), \
                patch('src.presentation.temporal.activities.activity.info',
                      return_value=SimpleNamespace(attempt=1)):
            from imager_config.loader import config
            from dataclasses import asdict
            with self.assertRaises(Exception):
                activities.render_image(_request(), asdict(config.temporal.render))
        executor.store.delete_file.assert_called_once_with(reference)

        # If the same object already became the canonical result, it stays.
        executor.store.delete_file.reset_mock()
        repository.collection.find_one.return_value = {'result': reference}
        activities._discard_unpublished_result('job_1', reference)
        executor.store.delete_file.assert_not_called()


def _request():
    return {
        'job_id': 'job_1',
        'source': {'bucket': 'imager', 'key': 'uploads/job_1/source.jpg',
                   'sha256': 'a' * 64, 'size_bytes': 100, 'content_type': 'image/jpeg'},
        'catalog_manifest': {'bucket': 'imager', 'key': 'catalog/manifests/cats/rev.json',
                             'sha256': 'b' * 64, 'size_bytes': 200, 'content_type': 'application/json'},
        'parameters': {'group_name': 'cats', 'insertion_format': 'crop', 'alpha_channel': 30,
                       'noise_level': 15, 'cell_size': 40, 'result_size': 120},
        'seed': 42,
    }
