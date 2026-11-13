import copy
import hashlib
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from imager_config.loader import config
from src.infrastructure.jobs.image_retention import ImageRetention


def value(document, path):
    for part in path.split('.'):
        if not isinstance(document, dict) or part not in document:
            return None
        document = document[part]
    return document


def matches(document, query):
    for key, expected in query.items():
        if key == '$or':
            if not any(matches(document, item) for item in expected):
                return False
            continue
        if key == '$nor':
            if any(matches(document, item) for item in expected):
                return False
            continue
        actual = value(document, key)
        if not isinstance(expected, dict):
            if actual != expected:
                return False
            continue
        for operator, operand in expected.items():
            if operator == '$ne' and actual == operand:
                return False
            if operator == '$in' and actual not in operand:
                return False
            if operator == '$nin' and actual in operand:
                return False
            if operator == '$exists' and (actual is not None) != operand:
                return False
            if operator == '$gt' and (actual is None or actual <= operand):
                return False
            if operator == '$lte' and (actual is None or actual > operand):
                return False
    return True


class Cursor:
    def __init__(self, rows):
        self.rows = copy.deepcopy(rows)

    def sort(self, order):
        for field, direction in reversed(order):
            self.rows.sort(key=lambda row: (value(row, field) is not None, value(row, field)), reverse=direction < 0)
        return self

    def limit(self, count):
        self.rows = self.rows[:count]
        return self

    def __iter__(self):
        return iter(self.rows)


class Collection:
    def __init__(self, rows):
        self.rows = {row['_id']: copy.deepcopy(row) for row in rows}
        self.before_claim = self.after_claim = None
        self.fail_ack_once = False

    def create_index(self, *args, **kwargs):
        pass

    def find(self, query):
        return Cursor([row for row in self.rows.values() if matches(row, query)])

    def find_one(self, query):
        return next(iter(self.find(query)), None)

    def update_one(self, query, update):
        if self.fail_ack_once and any(key.endswith('.state') and val == 'deleted' for key, val in update['$set'].items()):
            self.fail_ack_once = False
            raise OSError('Mongo acknowledgement unavailable')
        for row in self.rows.values():
            if not matches(row, query):
                continue
            for key, val in update['$set'].items():
                current = row
                parts = key.split('.')
                for part in parts[:-1]:
                    current = current.setdefault(part, {})
                current[parts[-1]] = copy.deepcopy(val)
            return SimpleNamespace(matched_count=1)
        return SimpleNamespace(matched_count=0)

    def find_one_and_update(self, query, update, **kwargs):
        if self.before_claim:
            callback, self.before_claim = self.before_claim, None
            callback(self.rows)
        updated = self.update_one(query, update)
        if not updated.matched_count:
            return None
        if self.after_claim:
            callback, self.after_claim = self.after_claim, None
            callback(self.rows)
        return self.find_one({'_id': query['_id']})


class Store:
    bucket = 'imager'

    def __init__(self, rows):
        self.objects = {}
        self.calls = []
        self.fail_key = None
        for row in rows:
            for reference in (row['request']['source'], row.get('result')):
                if reference:
                    self.objects[reference['key']] = reference

    def delete_file(self, reference):
        self.calls.append(reference['key'])
        if reference['key'] == self.fail_key:
            self.fail_key = None
            raise OSError('MinIO temporarily unavailable')
        self.objects.pop(reference['key'], None)


class ImageRetentionTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 13, tzinfo=timezone.utc)
        self.rows = [self.job(index) for index in range(20)]
        self.collection = Collection(self.rows)
        self.store = Store(self.rows)
        self.cleaner = ImageRetention(self.collection, self.store, clock=lambda: self.now)

    def job(self, index):
        job_id = f'job{index:02d}'
        digest = hashlib.sha256(job_id.encode()).hexdigest()
        source = {'bucket': 'imager', 'key': config.storage.source_key_template.format(job_id=job_id, sha256=digest),
                  'sha256': digest, 'size_bytes': 20, 'content_type': 'image/jpeg'}
        result = dict(source, key=config.storage.result_key_template.format(
            job_id=job_id, execution_token='token', result_filename=config.storage.result_filename))
        return {'_id': job_id, 'status': 'completed', 'created_at': self.now + timedelta(seconds=index),
                'completed_at': self.now + timedelta(seconds=index),
                'request': {'source': source, 'client_id': 'telegram'}, 'result': result,
                'execution': {'token': 'token'}, 'completion_outbox': {'state': 'sent'}}

    def remaining(self, kind):
        field = 'request.source' if kind == 'source' else 'result'
        return {row['_id'] for row in self.collection.rows.values() if value(row, field)['key'] in self.store.objects}

    def test_ten_sources_and_ten_results_are_ranked_independently(self):
        for index, row in enumerate(self.collection.rows.values()):
            row['completed_at'] = self.now - timedelta(seconds=index)
        immutable = {key: (copy.deepcopy(row['request']), copy.deepcopy(row['result'])) for key, row in self.collection.rows.items()}
        self.assertEqual(self.cleaner.cleanup_once(), {'source': 10, 'result': 10})
        self.assertEqual(self.remaining('source'), {f'job{i:02d}' for i in range(10, 20)})
        self.assertEqual(self.remaining('result'), {f'job{i:02d}' for i in range(10)})
        for key, row in self.collection.rows.items():
            self.assertEqual((row['request'], row['result']), immutable[key])
            self.assertEqual(row['status'], 'completed')

    def test_active_inputs_and_pending_telegram_delivery_are_protected(self):
        self.collection.rows['job00']['status'] = 'running'
        self.collection.rows['job01']['completion_outbox']['state'] = 'preparing'
        self.cleaner.cleanup_once()
        self.assertIn('job00', self.remaining('source'))
        self.assertIn('job01', self.remaining('result'))

    def test_non_telegram_pending_outbox_does_not_pin_smoke_results(self):
        self.collection.rows['job00']['request']['client_id'] = 'smoke'
        self.collection.rows['job00']['completion_outbox']['state'] = 'pending'
        self.cleaner.cleanup_once()
        self.assertNotIn('job00', self.remaining('result'))

    def test_shared_reference_is_retained_when_newer_job_uses_it(self):
        shared = self.job(99)
        shared['request']['source'] = copy.deepcopy(self.rows[0]['request']['source'])
        self.collection.rows[shared['_id']] = shared
        self.store.objects[shared['result']['key']] = shared['result']
        self.cleaner.cleanup_once()
        self.assertIn(self.rows[0]['request']['source']['key'], self.store.objects)

    def test_cross_kind_reference_is_also_protected(self):
        self.collection.rows['job19']['result'] = copy.deepcopy(self.rows[0]['request']['source'])
        self.cleaner.cleanup_once()
        self.assertIn(self.rows[0]['request']['source']['key'], self.store.objects)

    def test_same_job_alias_between_source_and_result_is_protected(self):
        self.collection.rows['job00']['result'] = copy.deepcopy(self.rows[0]['request']['source'])
        self.cleaner.cleanup_once()
        self.assertIn(self.rows[0]['request']['source']['key'], self.store.objects)

    def test_job_becoming_active_before_claim_cannot_be_deleted(self):
        self.collection.before_claim = lambda rows: rows['job00'].update(status='queued')
        self.cleaner.cleanup_once()
        self.assertIn('job00', self.remaining('source'))

    def test_delivery_claim_after_selection_is_rechecked(self):
        self.cleaner.policy = replace(config.retention, source_count=20)
        self.collection.after_claim = lambda rows: rows['job00']['completion_outbox'].update(state='pending')
        self.cleaner.cleanup_once()
        self.assertIn(self.rows[0]['result']['key'], self.store.objects)

    def test_catalog_reference_in_tampered_job_is_never_removed(self):
        reference = self.collection.rows['job00']['request']['source']
        reference['key'] = 'catalog/images/protected.png'
        self.store.objects[reference['key']] = reference
        self.cleaner.cleanup_once()
        self.assertIn(reference['key'], self.store.objects)
        self.assertEqual(value(self.collection.rows['job00'], 'artifact_lifecycle.source.state'), 'unmanaged')

    def test_minio_failure_is_recorded_and_retried_without_mutating_request(self):
        reference = self.rows[0]['request']['source']
        self.store.fail_key = reference['key']
        self.cleaner.cleanup_once()
        self.assertEqual(value(self.collection.rows['job00'], 'artifact_lifecycle.source.state'), 'pending')
        self.now += timedelta(seconds=config.retention.poll_seconds + 1)
        self.cleaner.cleanup_once()
        self.assertNotIn(reference['key'], self.store.objects)
        self.assertEqual(value(self.collection.rows['job00'], 'artifact_lifecycle.source.state'), 'deleted')

    def test_lost_mongo_ack_after_removal_is_idempotently_recovered(self):
        self.collection.fail_ack_once = True
        self.cleaner.cleanup_once()
        self.now += timedelta(seconds=config.retention.poll_seconds + 1)
        self.cleaner.cleanup_once()
        self.assertEqual(self.store.calls.count(self.rows[0]['request']['source']['key']), 2)
        self.assertEqual(value(self.collection.rows['job00'], 'artifact_lifecycle.source.state'), 'deleted')

    def test_bounded_pages_eventually_clean_a_backlog(self):
        policy = replace(config.retention, batch_size=3)
        cleaner = ImageRetention(self.collection, self.store, policy=policy, clock=lambda: self.now)
        for _ in range(10):
            cleaner.cleanup_once()
        self.assertEqual(len(self.remaining('source')), 10)
        self.assertEqual(len(self.remaining('result')), 10)

    def test_dry_run_does_not_change_metadata_or_storage(self):
        before = copy.deepcopy(self.collection.rows)
        self.assertEqual(self.cleaner.cleanup_once(dry_run=True), {'source': 10, 'result': 10})
        self.assertEqual(self.collection.rows, before)
        self.assertEqual(len(self.store.objects), 40)

    def test_newly_completed_result_is_not_deleted_from_an_older_rank_snapshot(self):
        original = self.cleaner._page
        added = self.job(99)

        def insert_then_page(kind):
            self.collection.rows.setdefault(added['_id'], copy.deepcopy(added))
            self.store.objects.setdefault(added['request']['source']['key'], added['request']['source'])
            self.store.objects.setdefault(added['result']['key'], added['result'])
            return original(kind)

        self.cleaner._page = insert_then_page
        self.cleaner.cleanup_once()
        self.assertIn(added['result']['key'], self.store.objects)

    def test_disabled_cleanup_does_nothing(self):
        cleaner = ImageRetention(self.collection, self.store, policy=replace(config.retention, enabled=False))
        self.assertEqual(cleaner.cleanup_once(), {'source': 0, 'result': 0})
        self.assertFalse(self.store.calls)
