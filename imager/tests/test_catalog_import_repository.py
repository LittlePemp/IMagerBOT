import copy
import hashlib
import unittest
from datetime import datetime, timedelta, timezone
from threading import RLock
from types import SimpleNamespace
from unittest.mock import patch

from pymongo.errors import DuplicateKeyError

from imager_contracts import ObjectRef
from imager_contracts.catalog_import import CatalogImportRequest, make_import_submission
from src.application.catalog_import.errors import CatalogImportBusy, CatalogImportConflict, InvalidCatalogArchive, LostCatalogImport
from src.infrastructure.catalog_import.mongo_imports import MongoImportRepository


class Collection:
    def __init__(self):
        self.documents = {}
        self.lock = RLock()
        self.before_update = None

    @staticmethod
    def value(document, key):
        for part in key.split('.'):
            if not isinstance(document, dict) or part not in document:
                return None
            document = document[part]
        return document

    @classmethod
    def matches(cls, document, query):
        for key, expected in query.items():
            if key == '$or':
                if not any(cls.matches(document, item) for item in expected):
                    return False
                continue
            actual = cls.value(document, key)
            if isinstance(expected, dict):
                for operator, value in expected.items():
                    if operator == '$in' and actual not in value:
                        return False
                    if operator == '$nin' and actual in value:
                        return False
                    if operator == '$lte' and (actual is None or actual > value):
                        return False
                    if operator == '$gt' and (actual is None or actual <= value):
                        return False
                    if operator == '$exists' and (actual is not None) != value:
                        return False
                    if operator not in ('$in', '$nin', '$lte', '$gt', '$exists'):
                        raise AssertionError(operator)
            elif actual != expected:
                return False
        return True

    @staticmethod
    def set(document, key, value):
        parts = key.split('.')
        for part in parts[:-1]:
            document = document.setdefault(part, {})
        document[parts[-1]] = copy.deepcopy(value)

    @classmethod
    def apply(cls, document, update, inserted=False):
        if inserted:
            for key, value in update.get('$setOnInsert', {}).items():
                cls.set(document, key, value)
        for key, value in update.get('$set', {}).items():
            cls.set(document, key, value)
        for key, value in update.get('$inc', {}).items():
            cls.set(document, key, (cls.value(document, key) or 0) + value)

    def create_index(self, *args, **kwargs):
        pass

    def find_one(self, query):
        with self.lock:
            return next((copy.deepcopy(doc) for doc in self.documents.values() if self.matches(doc, query)), None)

    def update_one(self, query, update, upsert=False):
        if self.before_update:
            hook, self.before_update = self.before_update, None
            hook(query, update)
        with self.lock:
            for doc in self.documents.values():
                if self.matches(doc, query):
                    self.apply(doc, update)
                    return SimpleNamespace(matched_count=1)
            if upsert:
                if query['_id'] in self.documents:
                    raise DuplicateKeyError('id')
                doc = {'_id': query['_id']}
                self.apply(doc, update, inserted=True)
                self.documents[doc['_id']] = doc
            return SimpleNamespace(matched_count=0)

    def find_one_and_update(self, query, update, **kwargs):
        with self.lock:
            for doc in self.documents.values():
                if self.matches(doc, query):
                    self.apply(doc, update)
                    return copy.deepcopy(doc)
        return None


def import_request(import_id='import_1'):
    return CatalogImportRequest(
        import_id, 123, 'pack_test', 'Тестовый пак',
        ObjectRef('imager', 'imports/source.zip', 'a' * 64, 100, 'application/zip'),
    ).to_dict()


def import_result():
    digest = hashlib.sha256(b'manifest').hexdigest()
    return {'group_name': 'pack_test', 'display_name': 'Тестовый пак', 'count': 2, 'revision': digest,
            'manifest': ObjectRef('imager', 'catalog/manifest.json', digest, 8, 'application/json').to_dict()}


class TestCatalogImportRepository(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 12, tzinfo=timezone.utc)
        self.jobs, self.catalogs = Collection(), Collection()
        self.repository = MongoImportRepository(self.jobs, self.catalogs, lease_seconds=60, clock=lambda: self.now)
        self.request = import_request()
        self.repository.receive(self.request)

    def test_same_inbox_identity_is_idempotent_and_changed_input_rejected(self):
        self.repository.receive(self.request)
        changed = copy.deepcopy(self.request)
        changed['display_name'] = 'Другой пак'
        with self.assertRaises(CatalogImportConflict):
            self.repository.receive(changed)
        self.assertEqual(len(self.jobs.documents), 1)

    def test_dispatch_snapshot_and_ack_are_fenced(self):
        event = self.repository.claim_dispatch()
        self.assertEqual(self.repository.validate_dispatch(event), self.request)
        self.assertFalse(self.repository.mark_dispatched('import_1', 'old', event['workflow_id']))
        self.assertTrue(self.repository.mark_dispatched('import_1', event['dispatch_token'], event['workflow_id']))
        event['execution_policy']['import']['maximum_attempts'] = True
        with self.assertRaises(CatalogImportConflict):
            self.repository.validate_dispatch(event)

    def test_one_live_execution_and_stale_token_cannot_publish_or_fail_new_attempt(self):
        first = self.repository.claim_execution(self.request)
        with self.assertRaises(CatalogImportBusy):
            self.repository.claim_execution(self.request)
        self.now += timedelta(seconds=61)
        second = self.repository.claim_execution(self.request)
        self.assertGreater(second['fence'], first['fence'])
        with self.assertRaises(LostCatalogImport):
            self.repository.publish_catalog('import_1', first['token'], import_result())
        self.assertFalse(self.repository.fail('import_1', first['token'], ValueError('old')))
        self.repository.heartbeat_execution('import_1', second['token'], {'phase': 'uploading', 'processed': 1, 'total': 2})
        self.assertEqual(self.repository.get('import_1')['progress']['phase'], 'uploading')

    def test_published_catalog_is_recovered_after_job_ack_loss(self):
        claim = self.repository.claim_execution(self.request)
        self.repository.publish_catalog('import_1', claim['token'], import_result())
        restored = self.repository.claim_execution(self.request)
        self.assertTrue(restored['already_completed'])
        self.assertEqual(restored['result'], import_result())
        self.assertEqual(self.repository.finalize_failure('import_1', 'Timeout')['status'], 'completed')

    def test_existing_catalog_is_create_only_and_never_overwritten(self):
        self.catalogs.documents['pack_test'] = dict(import_result(), _id='pack_test')
        before = copy.deepcopy(self.catalogs.documents)
        with self.assertRaises(CatalogImportConflict):
            self.repository.claim_execution(self.request)
        self.assertEqual(self.catalogs.documents, before)

    def test_terminal_tombstone_blocks_late_claim_that_cached_old_job(self):
        claim = self.repository.claim_execution(self.request)
        cached = self.repository.get('import_1')
        self.assertEqual(self.repository.finalize_failure('import_1', 'Terminated')['status'], 'failed')
        with patch.object(self.repository, 'get', return_value=cached):
            with self.assertRaises(CatalogImportConflict):
                self.repository.claim_execution(self.request)
            with self.assertRaises(LostCatalogImport):
                self.repository.publish_catalog('import_1', claim['token'], import_result())
        self.assertEqual(self.catalogs.documents['pack_test']['publication_state'], 'failed')

    def test_termination_before_first_claim_also_installs_terminal_fence(self):
        cached = self.repository.get('import_1')
        self.repository.finalize_failure('import_1', 'Terminated')
        with patch.object(self.repository, 'get', return_value=cached):
            with self.assertRaises(CatalogImportConflict):
                self.repository.claim_execution(self.request)

    def test_publication_winning_compensation_race_becomes_canonical_success(self):
        claim = self.repository.claim_execution(self.request)
        def race(query, update):
            # Compensation's upsert runs after its initial canonical read.
            self.repository.publish_catalog('import_1', claim['token'], import_result())
        self.catalogs.before_update = race
        outcome = self.repository.finalize_failure('import_1', 'Timeout')
        self.assertEqual(outcome, {'status': 'completed', 'result': import_result()})

    def test_safe_validation_error_persists_without_publishing_catalog(self):
        claim = self.repository.claim_execution(self.request)
        self.repository.fail('import_1', claim['token'], InvalidCatalogArchive('Архив повреждён.'))
        job = self.repository.get('import_1')
        self.assertEqual((job['status'], job['public_error']), ('failed', 'Архив повреждён.'))
        self.assertEqual(self.catalogs.documents['pack_test']['count'], 0)

    def test_generic_heartbeat_does_not_erase_public_import_phase(self):
        claim = self.repository.claim_execution(self.request)
        self.repository.heartbeat_execution('import_1', claim['token'], {'job_id': 'import_1', 'stage': 'starting'})
        self.assertEqual(self.repository.get('import_1')['progress']['phase'], 'downloading')
