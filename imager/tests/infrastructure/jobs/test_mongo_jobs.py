import copy
import unittest
from datetime import datetime, timedelta, timezone
from threading import RLock
from types import SimpleNamespace

from imager_contracts import JobRequest, make_submission, payload_fingerprint
from src.infrastructure.jobs.mongo_jobs import (
    JobBusy, JobConflict, LostExecution, MongoJobRepository,
)


def request(job_id='job_1'):
    return {
        'job_id': job_id,
        'source': {'bucket': 'imager', 'key': f'uploads/{job_id}/source.jpg',
                   'sha256': 'a' * 64, 'size_bytes': 100, 'content_type': 'image/jpeg'},
        'catalog_manifest': {'bucket': 'imager', 'key': 'catalog/manifests/cats/rev.json',
                             'sha256': 'b' * 64, 'size_bytes': 200, 'content_type': 'application/json'},
        'parameters': {'group_name': 'cats', 'insertion_format': 'crop', 'alpha_channel': 30,
                       'noise_level': 15, 'cell_size': 40, 'result_size': 120},
        'seed': 42, 'client_id': 'telegram', 'client_context': {'chat_id': 123},
        'schema_version': 1,
    }


def result_reference(token):
    return {'bucket': 'imager', 'key': f'jobs/job_1/attempts/{token}/result.jpg',
            'sha256': 'c' * 64, 'size_bytes': 1000, 'content_type': 'image/jpeg'}


class MemoryCollection:
    """Small Mongo-shaped fake with deep copies and atomic conditional updates."""

    def __init__(self):
        self.documents = {}
        self.updates = []
        self.lock = RLock()

    @staticmethod
    def value(document, path):
        value = document
        for part in path.split('.'):
            if not isinstance(value, dict) or part not in value:
                return None
            value = value[part]
        return value

    @classmethod
    def matches(cls, document, query):
        for key, expected in query.items():
            if key == '$or':
                if not any(cls.matches(document, option) for option in expected):
                    return False
                continue
            actual = cls.value(document, key)
            if isinstance(expected, dict):
                for operator, operand in expected.items():
                    if operator == '$in' and actual not in operand:
                        return False
                    if operator == '$nin' and actual in operand:
                        return False
                    if operator == '$lte' and (actual is None or actual > operand):
                        return False
                    if operator not in ('$in', '$nin', '$lte'):
                        raise AssertionError(f'Unsupported fake operator {operator}')
            elif actual != expected:
                return False
        return True

    @staticmethod
    def set_value(document, path, value):
        parts = path.split('.')
        current = document
        for part in parts[:-1]:
            current = current.setdefault(part, {})
        current[parts[-1]] = copy.deepcopy(value)

    @classmethod
    def apply(cls, document, update, inserted=False):
        if inserted:
            for key, value in update.get('$setOnInsert', {}).items():
                cls.set_value(document, key, value)
        for key, value in update.get('$set', {}).items():
            cls.set_value(document, key, value)
        for key, value in update.get('$inc', {}).items():
            cls.set_value(document, key, (cls.value(document, key) or 0) + value)

    def find_one(self, query):
        with self.lock:
            return next((copy.deepcopy(value) for value in self.documents.values()
                         if self.matches(value, query)), None)

    def update_one(self, query, update, upsert=False):
        with self.lock:
            self.updates.append((copy.deepcopy(query), copy.deepcopy(update)))
            for document in self.documents.values():
                if self.matches(document, query):
                    before = copy.deepcopy(document)
                    self.apply(document, update)
                    return SimpleNamespace(matched_count=1, modified_count=int(before != document))
            if upsert:
                document = dict(query)
                self.apply(document, update, inserted=True)
                self.documents[document['_id']] = document
            return SimpleNamespace(matched_count=0, modified_count=0)

    def find_one_and_update(self, query, update, sort=None, return_document=None):
        with self.lock:
            candidates = [value for value in self.documents.values() if self.matches(value, query)]
            for key, direction in reversed(sort or []):
                candidates.sort(key=lambda value: self.value(value, key), reverse=direction < 0)
            if not candidates:
                return None
            document = candidates[0]
            self.updates.append((copy.deepcopy(query), copy.deepcopy(update)))
            self.apply(document, update)
            return copy.deepcopy(document)

    def create_index(self, *args, **kwargs):
        return None


class TestMongoJobs(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 12, tzinfo=timezone.utc)
        self.collection = MemoryCollection()
        self.repository = MongoJobRepository(self.collection, lease_seconds=60, clock=lambda: self.now)
        self.request = request()

    def test_inbox_deduplicates_same_snapshot_and_rejects_changed_parameters(self):
        first = self.repository.receive(self.request)
        second = self.repository.receive(copy.deepcopy(self.request))
        self.assertEqual(first, second)
        self.assertEqual(len(self.collection.documents), 1)
        changed = copy.deepcopy(self.request)
        changed['parameters']['noise_level'] = 16
        with self.assertRaises(JobConflict):
            self.repository.receive(changed)
        self.assertEqual(self.collection.documents['job_1']['request']['parameters']['noise_level'], 15)

    def test_contract_snapshot_and_hash_do_not_depend_on_dict_order(self):
        reverse_order = dict(reversed(list(self.request.items())))
        self.assertEqual(payload_fingerprint(self.request), payload_fingerprint(reverse_order))
        document = make_submission(self.request, self.now)
        self.request['parameters']['cell_size'] = 75
        self.assertEqual(document['request']['parameters']['cell_size'], 40)
        for bad_id in ('../escape', 'job/id', '', 'job:id'):
            invalid = request(bad_id)
            with self.subTest(job_id=bad_id), self.assertRaises(ValueError):
                JobRequest.from_dict(invalid)

    def test_dispatch_lease_takeover_and_stale_acknowledgement(self):
        self.repository.receive(self.request)
        first = self.repository.claim_dispatch()
        self.assertIsNone(self.repository.claim_dispatch())
        self.now += timedelta(seconds=61)
        second = self.repository.claim_dispatch()
        self.assertNotEqual(first['dispatch_token'], second['dispatch_token'])
        self.assertEqual(second['attempts'], 2)
        self.assertFalse(self.repository.mark_dispatched('job_1', first['dispatch_token'], 'imager:job_1'))
        self.assertTrue(self.repository.mark_dispatched('job_1', second['dispatch_token'], 'imager:job_1'))
        self.assertIsNone(self.repository.claim_dispatch())

    def test_dispatch_failure_defers_delivery_without_losing_event(self):
        self.repository.receive(self.request)
        event = self.repository.claim_dispatch()
        self.assertTrue(self.repository.defer_dispatch('job_1', event['dispatch_token'], ConnectionError('offline')))
        self.assertIsNone(self.repository.claim_dispatch())
        self.now += timedelta(seconds=5)
        self.assertIsNotNone(self.repository.claim_dispatch())

    def test_live_claim_busy_then_takeover_fences_old_attempt(self):
        self.repository.receive(self.request)
        first = self.repository.claim_execution(self.request)
        with self.assertRaises(JobBusy):
            self.repository.claim_execution(self.request)
        self.now += timedelta(seconds=61)
        second = self.repository.claim_execution(self.request)
        self.assertNotEqual(first['token'], second['token'])
        self.assertEqual(second['fence'], first['fence'] + 1)
        with self.assertRaises(LostExecution):
            self.repository.heartbeat_execution('job_1', first['token'])
        with self.assertRaises(LostExecution):
            self.repository.complete('job_1', first['token'], result_reference(first['token']))
        self.assertFalse(self.repository.fail('job_1', first['token'], 'late failure'))
        self.assertFalse(self.repository.retry_execution('job_1', first['token'], 'late retry'))
        published = self.repository.complete('job_1', second['token'], result_reference(second['token']))
        self.assertEqual(self.collection.documents['job_1']['result'], published)

    def test_completion_and_outbox_are_one_conditional_update(self):
        self.repository.receive(self.request)
        claim = self.repository.claim_execution(self.request)
        count = len(self.collection.updates)
        reference = result_reference(claim['token'])
        self.repository.complete('job_1', claim['token'], reference)
        self.assertEqual(len(self.collection.updates), count + 1)
        query, update = self.collection.updates[-1]
        self.assertEqual(query['execution.token'], claim['token'])
        self.assertEqual(update['$set']['status'], 'completed')
        self.assertEqual(update['$set']['result'], reference)
        self.assertEqual(update['$set']['completion_outbox']['state'], 'pending')
        self.assertEqual(update['$set']['completion_outbox']['event_id'], 'job_1:completed')
        duplicate = self.repository.claim_execution(self.request)
        self.assertTrue(duplicate['already_completed'])
        self.assertEqual(duplicate['result'], reference)

    def test_heartbeat_extends_lease_and_records_progress(self):
        self.repository.receive(self.request)
        claim = self.repository.claim_execution(self.request)
        self.now += timedelta(seconds=30)
        self.repository.heartbeat_execution('job_1', claim['token'], {'stage': 'encoding'})
        document = self.collection.documents['job_1']
        self.assertEqual(document['execution']['lease_until'], self.now + timedelta(seconds=60))
        self.assertEqual(document['execution']['progress'], {'stage': 'encoding'})

    def test_retry_claim_uses_new_token_and_permanent_failure_publishes_event(self):
        self.repository.receive(self.request)
        first = self.repository.claim_execution(self.request)
        self.assertTrue(self.repository.retry_execution('job_1', first['token'], 'temporary'))
        second = self.repository.claim_execution(self.request)
        self.assertNotEqual(first['token'], second['token'])
        self.assertTrue(self.repository.fail('job_1', second['token'], 'invalid image'))
        document = self.collection.documents['job_1']
        self.assertEqual(document['status'], 'failed')
        self.assertEqual(document['completion_outbox']['event_id'], 'job_1:failed')
        with self.assertRaises(ValueError):
            self.repository.claim_execution(self.request)

    def test_execution_cannot_change_the_accepted_snapshot(self):
        self.repository.receive(self.request)
        changed = copy.deepcopy(self.request)
        changed['seed'] += 1
        with self.assertRaises(JobConflict):
            self.repository.claim_execution(changed)
        self.assertEqual(self.collection.documents['job_1']['status'], 'queued')

    def test_workflow_failure_compensation_fences_a_crashed_attempt(self):
        self.repository.receive(self.request)
        claim = self.repository.claim_execution(self.request)
        outcome = self.repository.finalize_failure('job_1', 'Activity timed out')
        self.assertEqual(outcome['status'], 'failed')
        self.assertIsNone(self.collection.documents['job_1']['execution']['token'])
        self.assertEqual(self.collection.documents['job_1']['completion_outbox']['state'], 'pending')
        with self.assertRaises(LostExecution):
            self.repository.complete('job_1', claim['token'], result_reference(claim['token']))

    def test_failure_compensation_does_not_overwrite_already_published_success(self):
        self.repository.receive(self.request)
        claim = self.repository.claim_execution(self.request)
        reference = result_reference(claim['token'])
        self.repository.complete('job_1', claim['token'], reference)
        before = copy.deepcopy(self.collection.documents['job_1'])
        outcome = self.repository.finalize_failure('job_1', 'Completion reply was lost')
        self.assertEqual(outcome, {'status': 'completed', 'result': reference})
        self.assertEqual(self.collection.documents['job_1'], before)

    def test_dispatch_rejection_atomically_publishes_failed_outbox(self):
        self.repository.receive(self.request)
        event = self.repository.claim_dispatch()
        self.assertEqual(self.repository.validate_dispatch(event), JobRequest.from_dict(self.request).to_dict())
        corrupt = copy.deepcopy(event)
        corrupt['request']['seed'] += 1
        with self.assertRaises(JobConflict):
            self.repository.validate_dispatch(corrupt)
        self.assertFalse(self.repository.reject_dispatch('job_1', 'stale-token', 'bad'))
        self.assertTrue(self.repository.reject_dispatch('job_1', event['dispatch_token'], 'bad input'))
        stored = self.collection.documents['job_1']
        self.assertEqual(stored['status'], 'failed')
        self.assertEqual(stored['dispatch']['state'], 'rejected')
        self.assertEqual(stored['completion_outbox']['event_id'], 'job_1:failed')
