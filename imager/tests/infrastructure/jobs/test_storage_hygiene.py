import copy
import unittest
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

from pymongo.errors import NetworkTimeout

from imager_config.loader import config

from src.infrastructure.jobs.storage_hygiene import StorageHygiene, archive_prefix, result_job_id


class Cursor(list):
    def sort(self, *args):
        return self

    def limit(self, count):
        return Cursor(self[:count])


_MISSING = object()


class JobDocuments:
    """Mongo selector semantics for these source/result and purge regressions."""
    def __init__(self, rows):
        self.rows = copy.deepcopy(rows)

    @staticmethod
    def _value(row, path):
        for part in path.split('.'):
            if not isinstance(row, dict) or part not in row:
                return _MISSING
            row = row[part]
        return row

    @classmethod
    def _matches(cls, row, query):
        for field, expected in query.items():
            if field == '$and':
                if not all(cls._matches(row, item) for item in expected):
                    return False
                continue
            if field == '$or':
                if not any(cls._matches(row, item) for item in expected):
                    return False
                continue
            value = cls._value(row, field)
            actual = None if value is _MISSING else value
            if not isinstance(expected, dict):
                if actual != expected:
                    return False
                continue
            for operator, operand in expected.items():
                if operator == '$in' and actual not in operand:
                    return False
                if operator == '$nin' and actual in operand:
                    return False
                if operator == '$ne' and actual == operand:
                    return False
                if operator == '$lt' and (actual is None or actual >= operand):
                    return False
                if operator == '$exists' and (value is not _MISSING) != operand:
                    return False
                if operator not in ('$in', '$nin', '$ne', '$lt', '$exists'):
                    raise AssertionError(f'Unsupported selector operator: {operator}')
        return True

    def find_one(self, query, projection=None):
        return next((copy.deepcopy(row) for row in self.rows if self._matches(row, query)), None)

    def delete_many(self, query):
        kept = [row for row in self.rows if not self._matches(row, query)]
        count = len(self.rows) - len(kept)
        self.rows = kept
        return SimpleNamespace(deleted_count=count)


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
        self.hygiene.jobs = JobDocuments([
            {'_id': 'live', 'status': 'running'},
            {'_id': 'done', 'status': 'completed',
             'result': {'bucket': 'imager', 'key': 'jobs/done/attempts/t2/result.jpg'}},
        ])
        self.store.reference_for_key.side_effect = lambda key: {'bucket': 'imager', 'key': key}
        self.assertEqual(self.hygiene.cleanup_orphan_results(), 1)
        self.store.delete_file.assert_called_once_with({'bucket': 'imager', 'key': 'jobs/done/attempts/t0/result.jpg'})

    def test_another_job_source_or_result_pins_an_old_attempt(self):
        reference = {'bucket': 'imager', 'key': 'jobs/done/attempts/old/result.jpg'}
        self.store.list_objects.return_value = [(reference['key'], self.now - timedelta(days=1))]
        self.store.reference_for_key.return_value = reference
        for field in ('source', 'result'):
            with self.subTest(reference_type=field):
                other = {'_id': 'other', 'status': 'running'}
                if field == 'source':
                    other['request'] = {'source': reference}
                else:
                    other.update(status='completed', result=reference)
                self.hygiene.jobs = JobDocuments([
                    {'_id': 'done', 'status': 'failed', 'result': None}, other,
                ])
                self.assertEqual(self.hygiene.cleanup_orphan_results(), 0)
        self.store.delete_file.assert_not_called()

    def test_document_purge_requires_terminal_state_and_released_artifacts(self):
        self.jobs.delete_many.return_value = SimpleNamespace(deleted_count=3)
        self.assertEqual(self.hygiene.purge_job_documents(), 3)
        query = self.jobs.delete_many.call_args.args[0]
        self.assertEqual(query['status'], {'$in': ['completed', 'failed']})
        self.assertIn('$lt', query['updated_at'])
        self.assertEqual(query['completion_outbox.state'], {'$nin': ['pending', 'preparing', 'claimed']})
        self.assertEqual(len(query['$and']), 3)

    def test_purge_waits_for_history_refund_ack_and_actual_artifact_removal(self):
        def old_job(name):
            return {
                '_id': name, 'status': 'failed', 'updated_at': self.now - timedelta(days=31),
                'request': {'client_id': 'telegram', 'source': {'key': 'uploads/source'},
                            'client_context': {'history_record_id': 'history-' + name}},
                'result': None, 'artifact_lifecycle': {'source': {'state': 'deleted'}},
                'completion_outbox': {'state': 'sent'},
            }
        rows = []
        for state in (None, 'pending', 'claimed', 'synced'):
            row = old_job(str(state))
            if state is not None:
                row['telegram_history_projection'] = {'state': state}
            rows.append(row)
        unmanaged_source = old_job('unmanaged-source')
        unmanaged_source['artifact_lifecycle']['source']['state'] = 'unmanaged'
        unmanaged_source['telegram_history_projection'] = {'state': 'synced'}
        unmanaged_result = old_job('unmanaged-result')
        unmanaged_result['result'] = {'key': 'unknown/result'}
        unmanaged_result['artifact_lifecycle']['result'] = {'state': 'unmanaged'}
        unmanaged_result['telegram_history_projection'] = {'state': 'synced'}
        legacy = old_job('legacy')
        legacy['request']['client_context'] = {}
        external = old_job('external')
        external['request']['client_id'] = 'another-client'
        documents = JobDocuments(rows + [unmanaged_source, unmanaged_result, legacy, external])
        self.hygiene.jobs = documents
        self.assertEqual(self.hygiene.purge_job_documents(), 3)
        self.assertEqual({row['_id'] for row in documents.rows}, {
            'None', 'pending', 'claimed', 'unmanaged-source', 'unmanaged-result',
        })

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
        repository.collection.find_one.side_effect = lambda query, projection: (
            {'status': 'running', 'execution': {'token': 'new-owner'}, 'result': None}
            if '_id' in query else None
        )
        executor = Mock()
        executor.execute.return_value = reference
        activities = RenderActivities(repository, executor)
        with patch('src.presentation.temporal.activities.activity.heartbeat'), \
                patch('src.presentation.temporal.activities.activity.is_cancelled', return_value=False), \
                patch('src.presentation.temporal.activities.activity.info',
                      return_value=SimpleNamespace(attempt=1)):
            with self.assertRaises(Exception):
                activities.render_image(_request(), asdict(config.temporal.render))
        executor.store.delete_file.assert_called_once_with(reference)

        # If the same object already became the canonical result, it stays.
        executor.store.delete_file.reset_mock()
        repository.collection.find_one.side_effect = None
        repository.collection.find_one.return_value = {'result': reference}
        activities._discard_unpublished_result('job_1', 'token', reference)
        executor.store.delete_file.assert_not_called()

    def test_same_or_unknown_owner_defers_cleanup_until_outcome_is_certain(self):
        from src.presentation.temporal.activities import RenderActivities
        reference = {'bucket': 'imager', 'key': 'jobs/job_1/attempts/token/result.jpg'}
        repository, executor = Mock(), Mock()
        activity = RenderActivities(repository, executor)
        for current in (
            None, {'result': None},
            {'status': 'running', 'execution': {'token': 'token'}, 'result': None},
            {'status': 'retrying', 'execution': {'token': 'token'}, 'result': None},
            {'status': 'running', 'execution': {}, 'result': None},
        ):
            with self.subTest(current=current):
                repository.collection.find_one.return_value = current
                activity._discard_unpublished_result('job_1', 'token', reference)
        executor.store.delete_file.assert_not_called()

    def test_fenced_attempt_referenced_by_another_source_is_kept(self):
        from src.presentation.temporal.activities import RenderActivities
        reference = {'bucket': 'imager', 'key': 'jobs/job_1/attempts/token/result.jpg'}
        repository, executor = Mock(), Mock()
        repository.collection = JobDocuments([
            {'_id': 'job_1', 'status': 'failed', 'result': None},
            {'_id': 'other', 'status': 'running', 'request': {'source': reference}},
        ])
        RenderActivities(repository, executor)._discard_unpublished_result('job_1', 'token', reference)
        executor.store.delete_file.assert_not_called()

    def test_late_complete_after_network_timeout_keeps_the_canonical_object(self):
        from src.presentation.temporal.activities import RenderActivities
        reference = {'bucket': 'imager', 'key': 'jobs/job_1/attempts/token/result.jpg',
                     'sha256': 'c' * 64, 'size_bytes': 10, 'content_type': 'image/jpeg'}
        current = {'_id': 'job_1', 'status': 'running', 'execution': {'token': 'token'}, 'result': None}
        objects = {reference['key']}
        repository, executor = Mock(), Mock()

        def claim(request):
            if current['status'] == 'completed':
                return {'already_completed': True, 'result': current['result'], 'token': None}
            return {'already_completed': False, 'token': 'token', 'fence': 1}

        def read_before_late_commit(query, projection):
            old_response = copy.deepcopy(current)
            # An earlier update can finish after a client NetworkTimeout and
            # this read's snapshot, before the caller would issue MinIO DELETE.
            current.update(status='completed', result=copy.deepcopy(reference))
            return old_response

        repository.claim_execution.side_effect = claim
        repository.complete.side_effect = NetworkTimeout('Completion write outcome is unknown')
        repository.collection.find_one.side_effect = read_before_late_commit
        executor.execute.return_value = reference
        executor.store.delete_file.side_effect = lambda ref: objects.remove(ref['key'])
        activity = RenderActivities(repository, executor)
        with patch('src.presentation.temporal.activities.LeaseHeartbeat'), \
                patch('src.presentation.temporal.activities.activity.info', return_value=SimpleNamespace(attempt=1)):
            with self.assertRaises(Exception):
                activity.render_image(_request(), asdict(config.temporal.render))
            self.assertEqual(current['status'], 'completed')
            self.assertIn(current['result']['key'], objects)
            self.assertEqual(activity.render_image(_request(), asdict(config.temporal.render)), reference)
        executor.execute.assert_called_once()
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
