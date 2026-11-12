import asyncio
import copy
import hashlib
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

from pymongo.errors import DuplicateKeyError, NetworkTimeout

from imager_contracts import make_submission
from src.application.admission import AdmissionDispatcher
from src.infrastructure.data.errors import AdmissionConflict, DailyQuotaExceeded
from src.infrastructure.data.repositories.admission_repository import AdmissionRepository


def submission(job_id, user_id=1, noise=15):
    def reference(key):
        return {'bucket': 'imager', 'key': key, 'size_bytes': 6,
                'sha256': hashlib.sha256(b'pixels').hexdigest(), 'content_type': 'image/jpeg'}
    return make_submission({
        'job_id': job_id, 'source': reference(f'uploads/{job_id}/source.jpg'),
        'parameters': {'group_name': 'animals', 'insertion_format': 'crop',
                       'alpha_channel': 30, 'noise_level': noise, 'cell_size': 40, 'result_size': 120},
        'catalog_manifest': reference('catalog/animals/manifest.json'), 'seed': 123,
        'client_id': 'telegram', 'client_context': {'user_id': user_id, 'chat_id': user_id},
    })


def matches(document, query):
    for key, condition in query.items():
        if key == '$or':
            if not any(matches(document, item) for item in condition):
                return False
            continue
        if key == 'admissions.job_id':
            values = [entry['job_id'] for entry in document.get('admissions', [])]
            if isinstance(condition, dict) and '$ne' in condition:
                if condition['$ne'] in values:
                    return False
            elif condition not in values:
                return False
            continue
        value = document.get(key)
        if isinstance(condition, dict):
            if '$elemMatch' in condition and not any(matches(item, condition['$elemMatch']) for item in value):
                return False
            if '$lt' in condition and not value < condition['$lt']:
                return False
            if '$lte' in condition and not value <= condition['$lte']:
                return False
        elif value != condition:
            return False
    return True


class AtomicCollection:
    """Small atomic-document model; assertions exercise the actual Mongo filters."""
    def __init__(self):
        self.documents = {}
        self.lock = asyncio.Lock()
        self.indexes = []
        self.fail_after_write = False

    def with_options(self, **kwargs):
        self.write_concern = kwargs['write_concern']
        return self

    async def create_index(self, *args, **kwargs):
        self.indexes.append((args, kwargs))

    async def find_one(self, query, projection=None):
        async with self.lock:
            return copy.deepcopy(next((doc for doc in self.documents.values() if matches(doc, query)), None))

    def _apply(self, document, query, update):
        entry = None
        if 'admissions' in query:
            entry = next(item for item in document['admissions'] if matches(item, query['admissions']['$elemMatch']))
        for operation, values in update.items():
            if operation == '$setOnInsert':
                continue
            for key, value in values.items():
                target = entry if key.startswith('admissions.$.') else document
                field = key.removeprefix('admissions.$.')
                if operation == '$set':
                    target[field] = copy.deepcopy(value)
                elif operation == '$inc':
                    target[field] = target.get(field, 0) + value
                elif operation == '$push':
                    if any(item['job_id'] == value['job_id']
                           for bucket in self.documents.values() for item in bucket['admissions']):
                        raise DuplicateKeyError('job identity')
                    target[field].append(copy.deepcopy(value))

    async def update_one(self, query, update, upsert=False):
        async with self.lock:
            document = next((doc for doc in self.documents.values() if matches(doc, query)), None)
            if document is None and upsert:
                document = copy.deepcopy(update['$setOnInsert'])
                self.documents[document['_id']] = document
            if document is not None:
                self._apply(document, query, update)
            return SimpleNamespace(matched_count=int(document is not None))

    async def find_one_and_update(self, query, update, **kwargs):
        async with self.lock:
            original = next((doc for doc in self.documents.values() if matches(doc, query)), None)
            if original is None:
                return None
            candidate = copy.deepcopy(original)
            self._apply(candidate, query, update)
            self.documents[candidate['_id']] = candidate
            if self.fail_after_write:
                self.fail_after_write = False
                raise NetworkTimeout('ack lost after commit')
            return copy.deepcopy(candidate)

    async def aggregate(self, pipeline):
        pending = sum(entry['state'] in ('pending', 'claimed')
                      for bucket in self.documents.values() for entry in bucket['admissions'])
        return SimpleNamespace(to_list=AsyncMock(return_value=[{'count': pending}] if pending else []))


class TestDailyQuota(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.collection = AtomicCollection()
        self.repository = AdmissionRepository(
            self.collection, timezone='Europe/Moscow', daily_limit=5, lease_seconds=60, retry_seconds=3,
        )
        self.now = datetime(2026, 11, 12, 12, tzinfo=timezone.utc)

    async def test_one_remaining_slot_allows_exactly_one_concurrent_distinct_photo(self):
        for index in range(4):
            await self.repository.accept_submission(submission(f'job-{index}'), actor_id=1, now=self.now)
        results = await asyncio.gather(*[
            self.repository.accept_submission(submission(f'last-{index}'), actor_id=1, now=self.now)
            for index in range(2)
        ], return_exceptions=True)
        self.assertEqual(sum(isinstance(result, DailyQuotaExceeded) for result in results), 1)
        self.assertEqual(await self.repository.remaining(1, self.now), 0)
        bucket = next(iter(self.collection.documents.values()))
        self.assertEqual(bucket['used'], 5)
        self.assertEqual(len(bucket['admissions']), 5)

    async def test_concurrent_duplicate_costs_one_slot(self):
        results = await asyncio.gather(*[
            self.repository.accept_submission(submission('same-job'), actor_id=1, now=self.now)
            for _ in range(2)
        ])
        self.assertEqual(sum(not receipt.duplicate for receipt in results), 1)
        self.assertTrue(all(receipt.actor_id == 1 for receipt in results))
        self.assertEqual(await self.repository.remaining(1, self.now), 4)

    async def test_calendar_reset_is_moscow_midnight_and_replay_does_not_charge_new_day(self):
        before = datetime(2026, 11, 12, 20, 59, tzinfo=timezone.utc)
        after = before + timedelta(minutes=2)
        await self.repository.accept_submission(submission('yesterday'), actor_id=1, now=before)
        receipt = await self.repository.accept_submission(submission('today'), actor_id=1, now=after)
        self.assertEqual(receipt.date, '2026-11-13')
        duplicate = await self.repository.accept_submission(submission('yesterday'), actor_id=1, now=after)
        self.assertTrue(duplicate.duplicate)
        self.assertEqual(duplicate.date, '2026-11-12')
        self.assertEqual(await self.repository.remaining(1, after), 4)
        self.assertEqual(self.repository.day_window(after), (
            datetime(2026, 11, 12, 21, tzinfo=timezone.utc), datetime(2026, 11, 13, 21, tzinfo=timezone.utc),
        ))

    async def test_same_job_with_changed_payload_is_rejected_without_spending_again(self):
        await self.repository.accept_submission(submission('job'), actor_id=1, now=self.now)
        with self.assertRaises(AdmissionConflict):
            await self.repository.accept_submission(submission('job', noise=20), actor_id=1, now=self.now)
        self.assertEqual(await self.repository.remaining(1, self.now), 4)

    async def test_lost_ack_is_reconciled_and_daily_seed_is_never_overwritten(self):
        await self.repository.ensure_day(1, initial_used=4, now=self.now)
        self.collection.fail_after_write = True
        receipt = await self.repository.accept_submission(submission('job'), actor_id=1, now=self.now)
        self.assertTrue(receipt.duplicate)
        await self.repository.ensure_day(1, initial_used=0, now=self.now)
        self.assertEqual(await self.repository.remaining(1, self.now), 0)

    async def test_expired_outbox_claim_is_reclaimed_and_old_token_cannot_ack(self):
        await self.repository.accept_submission(submission('job'), actor_id=1, now=self.now)
        first = await self.repository.claim(self.now)
        self.assertIsNone(await self.repository.claim(self.now + timedelta(seconds=59)))
        second = await self.repository.claim(self.now + timedelta(seconds=61))
        self.assertNotEqual(first.token, second.token)
        self.assertFalse(await self.repository.acknowledge(first.job_id, first.token))
        self.assertTrue(await self.repository.acknowledge(second.job_id, second.token))
        self.assertEqual((await self.repository.find('job')).state, 'sent')

    async def test_write_concern_and_cross_day_job_index_are_explicit(self):
        await self.repository.initialize_indexes()
        self.assertEqual(self.collection.write_concern.document, {'j': True, 'w': 1})
        self.assertTrue(self.collection.indexes[0][1]['unique'])
        self.assertEqual(self.collection.indexes[0][0], ('admissions.job_id',))

    async def test_global_backpressure_count_includes_claimed_but_not_published_submissions(self):
        await self.repository.accept_submission(submission('job-1'), actor_id=1, now=self.now)
        await self.repository.accept_submission(submission('job-2', user_id=2), actor_id=2, now=self.now)
        claim = await self.repository.claim(self.now)
        self.assertEqual(await self.repository.pending_count(), 2)
        await self.repository.acknowledge(claim.job_id, claim.token)
        self.assertEqual(await self.repository.pending_count(), 1)

    async def test_dispatcher_replays_identical_snapshot_after_failure(self):
        document = submission('job')
        await self.repository.accept_submission(document, actor_id=1)
        gateway = SimpleNamespace(publish_submission=AsyncMock(side_effect=RuntimeError('core down')))
        dispatcher = AdmissionDispatcher(self.repository, gateway)
        self.assertTrue(await dispatcher.deliver_once())
        self.assertEqual(gateway.publish_submission.await_args.args[0], document)
        bucket = next(iter(self.collection.documents.values()))
        self.assertEqual(bucket['used'], 1)
        self.assertEqual(bucket['admissions'][0]['state'], 'pending')
        bucket['admissions'][0]['next_attempt_at'] = datetime.now(timezone.utc) - timedelta(seconds=1)
        gateway.publish_submission.side_effect = None
        self.assertTrue(await dispatcher.deliver_once())
        self.assertEqual(gateway.publish_submission.await_args.args[0], document)
        self.assertEqual((await self.repository.find('job')).state, 'sent')

    async def test_publication_and_backpressure_acceptance_share_one_local_critical_section(self):
        await self.repository.accept_submission(submission('job'), actor_id=1)
        lock = asyncio.Lock()
        publishing = asyncio.Event()
        release = asyncio.Event()
        checked = asyncio.Event()

        async def publish(document):
            self.assertTrue(lock.locked())
            publishing.set()
            await release.wait()

        async def check_and_accept():
            async with lock:
                checked.set()

        dispatcher = AdmissionDispatcher(
            self.repository, SimpleNamespace(publish_submission=publish), publication_lock=lock,
        )
        publication = asyncio.create_task(dispatcher.deliver_once())
        await publishing.wait()
        acceptance = asyncio.create_task(check_and_accept())
        await asyncio.sleep(0)
        self.assertFalse(checked.is_set())
        release.set()
        await asyncio.gather(publication, acceptance)
        self.assertTrue(checked.is_set())
