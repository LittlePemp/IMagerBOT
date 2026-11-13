"""Durable fanout survives replay, concurrent consumers and unknown sends."""
import asyncio
import copy
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from imager_config.loader import config
from src.infrastructure.data.repositories.admission_repository import AdmissionRepository
from test_daily_quota import AtomicCollection, submission


_MISSING = object()


def values_at(document, path):
    parts = path.split('.') if isinstance(path, str) else path
    if not parts:
        return [document]
    if isinstance(document, list):
        return [value for row in document for value in values_at(row, parts)]
    if not isinstance(document, dict) or parts[0] not in document:
        return [_MISSING]
    return values_at(document[parts[0]], parts[1:])


def matches(document, query):
    for field, condition in query.items():
        if field == '$or':
            if not any(matches(document, branch) for branch in condition):
                return False
            continue
        values = values_at(document, field)
        if isinstance(condition, dict) and any(key.startswith('$') for key in condition):
            for operator, operand in condition.items():
                if operator == '$elemMatch':
                    if not any(isinstance(value, list) and any(matches(row, operand) for row in value)
                               for value in values):
                        return False
                elif operator == '$ne':
                    if any(value == operand for value in values):
                        return False
                elif operator == '$lt':
                    if not any(value is not _MISSING and value < operand for value in values):
                        return False
                elif operator == '$lte':
                    if not any(value is not _MISSING and value <= operand for value in values):
                        return False
                else:
                    raise AssertionError(f'Unsupported model operator: {operator}')
        elif condition is None:
            if not any(value is None or value is _MISSING for value in values):
                return False
        elif not any(value == condition for value in values):
            return False
    return True


def assign(document, path, value):
    parts = path.split('.')
    for part in parts[:-1]:
        document = document.setdefault(part, {})
    document[parts[-1]] = copy.deepcopy(value)


class NoticeCollection(AtomicCollection):
    """Atomic in-memory Mongo model with dotted fields and aggregation stages."""
    async def find_one(self, query, projection=None, sort=None):
        async with self.lock:
            rows = [row for row in self.documents.values() if matches(row, query)]
            return copy.deepcopy(rows[0] if rows else None)

    def _apply(self, document, query, update):
        entry = None
        if 'admissions' in query:
            entry = next(row for row in document['admissions']
                         if matches(row, query['admissions']['$elemMatch']))
        for operation, changes in update.items():
            if operation == '$setOnInsert':
                continue
            for field, value in changes.items():
                target = entry if field.startswith('admissions.$.') else document
                path = field.removeprefix('admissions.$.')
                previous = values_at(target, path)[0]
                if operation == '$set':
                    assign(target, path, value)
                elif operation == '$min':
                    if previous is _MISSING or value < previous:
                        assign(target, path, value)
                elif operation == '$inc':
                    assign(target, path, (0 if previous is _MISSING else previous) + value)
                elif operation == '$push':
                    previous.append(copy.deepcopy(value))
                else:
                    raise AssertionError(f'Unsupported model update: {operation}')

    async def update_one(self, query, update, upsert=False):
        async with self.lock:
            row = next((row for row in self.documents.values() if matches(row, query)), None)
            if row is None and upsert:
                row = copy.deepcopy(update['$setOnInsert'])
                self.documents[row['_id']] = row
            if row is not None:
                self._apply(row, query, update)
            return SimpleNamespace(matched_count=int(row is not None))

    async def aggregate(self, pipeline):
        rows = copy.deepcopy(list(self.documents.values()))
        for stage in pipeline:
            if '$match' in stage:
                rows = [row for row in rows if matches(row, stage['$match'])]
            elif '$unwind' in stage:
                path = stage['$unwind'].removeprefix('$')
                result = []
                for row in rows:
                    for entry in values_at(row, path)[0]:
                        copy_row = copy.deepcopy(row)
                        assign(copy_row, path, entry)
                        result.append(copy_row)
                rows = result
            elif '$sort' in stage:
                for field, direction in reversed(list(stage['$sort'].items())):
                    rows.sort(key=lambda row: values_at(row, field)[0], reverse=direction < 0)
            elif '$limit' in stage:
                rows = rows[:stage['$limit']]
            elif '$project' in stage:
                rows = [{key: values_at(row, value.removeprefix('$') if isinstance(value, str) else key)[0]
                         for key, value in stage['$project'].items() if value != 0} for row in rows]
            else:
                raise AssertionError(f'Unsupported model stage: {stage}')
        return SimpleNamespace(to_list=AsyncMock(return_value=rows))


class AdminNoticeRepositoryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.collection = NoticeCollection()
        self.repository = self.new_repository()
        self.now = datetime(2026, 11, 13, 12, tzinfo=timezone.utc)

    def new_repository(self):
        return AdmissionRepository(
            self.collection, timezone='Europe/Moscow', daily_limit=5, lease_seconds=60, retry_seconds=3,
        )

    def entry(self, job_id):
        return next(entry for bucket in self.collection.documents.values()
                    for entry in bucket['admissions'] if entry['job_id'] == job_id)

    async def accept(self, job_id='new', *, actor_id=1, now=None, published=True):
        receipt = await self.repository.accept_submission(
            submission(job_id, actor_id), actor_id=actor_id, now=now or self.now,
        )
        if published:
            self.entry(job_id)['state'] = 'sent'
        return receipt

    async def initialize(self, job_id='new', recipients=(99, 100)):
        await self.accept(job_id)
        self.assertTrue(await self.repository.initialize_admin_notice(job_id, recipients, now=self.now))

    async def test_notice_is_accepted_atomically_and_duplicate_never_resets_fanout(self):
        # An acknowledgement can be lost after the Mongo write committed.
        self.collection.fail_after_write = True
        receipt = await self.accept()
        self.assertTrue(receipt.duplicate)
        notice = self.entry('new')['admin_notice']
        self.assertEqual(notice, {'state': 'pending', 'recipients': None, 'next_attempt_at': self.now})
        await self.repository.initialize_admin_notice('new', [99], now=self.now)
        await self.repository.claim_admin_notice_recipient('new', 99, now=self.now)
        await self.repository.update_admin_notice_recipient('new', 99, state='sent', message_id=17, now=self.now)
        await self.repository.complete_admin_notice('new', now=self.now)
        snapshot = copy.deepcopy(self.entry('new'))
        self.assertTrue((await self.accept()).duplicate)
        self.assertEqual(self.entry('new'), snapshot)
        self.assertIsNone(await self.repository.next_admin_notice(now=self.now))

    async def test_legacy_and_unpublished_admissions_are_not_notified(self):
        await self.accept('legacy')
        del self.entry('legacy')['admin_notice']
        await self.accept('waiting', published=False)
        await self.accept('claimed', published=False)
        self.entry('claimed')['state'] = 'claimed'
        self.assertIsNone(await self.repository.next_admin_notice(now=self.now))
        self.entry('waiting')['state'] = 'sent'
        found = await self.repository.next_admin_notice(now=self.now)
        self.assertEqual(found['job_id'], 'waiting')
        self.assertEqual(found['actor_id'], 1)
        self.assertEqual(found['document']['request']['parameters']['noise_level'], 15)

    async def test_notice_order_uses_entry_acceptance_time_across_buckets(self):
        await self.accept('first', actor_id=1)
        await self.accept('other', actor_id=2, now=self.now + timedelta(seconds=1))
        await self.accept('latest', actor_id=1, now=self.now + timedelta(seconds=2))
        await self.repository.initialize_admin_notice('first', [], now=self.now)
        self.assertTrue(await self.repository.complete_admin_notice('first', now=self.now))
        self.assertEqual((await self.repository.next_admin_notice(self.now + timedelta(seconds=5)))['job_id'], 'other')

    async def test_fanout_is_frozen_and_concurrent_senders_reserve_one_attempt(self):
        await self.initialize(recipients=(99, 99, 100))
        self.assertFalse(await self.repository.initialize_admin_notice('new', [101], now=self.now))
        attempts = await asyncio.gather(*[
            self.repository.claim_admin_notice_recipient('new', 99, now=self.now) for _ in range(12)
        ])
        self.assertEqual(sum(attempts), 1)
        self.assertEqual(set(self.entry('new')['admin_notice']['recipients']), {'99', '100'})
        self.assertEqual(self.entry('new')['admin_notice']['recipients']['99']['state'], 'uncertain')
        self.assertFalse(await self.repository.complete_admin_notice('new', now=self.now))
        self.assertTrue(await self.repository.claim_admin_notice_recipient('new', 100, now=self.now))
        await self.repository.update_admin_notice_recipient('new', 100, state='sent', message_id=18, now=self.now)
        self.assertTrue(await self.repository.complete_admin_notice('new', now=self.now))
        # A fresh process cannot retry a request whose outcome was unknown.
        restarted = self.new_repository()
        self.assertFalse(await restarted.claim_admin_notice_recipient('new', 99, now=self.now + timedelta(days=1)))
        self.assertIsNone(await restarted.next_admin_notice(now=self.now + timedelta(days=1)))

    async def test_definite_floodwait_retry_does_not_repeat_sent_siblings_or_starve_new_work(self):
        await self.initialize()
        await self.repository.claim_admin_notice_recipient('new', 99, now=self.now)
        await self.repository.update_admin_notice_recipient('new', 99, state='sent', message_id=19, now=self.now)
        await self.repository.claim_admin_notice_recipient('new', 100, now=self.now)
        retry_at = self.now + timedelta(minutes=1)
        await self.repository.update_admin_notice_recipient('new', 100, state='pending', retry_at=retry_at, now=self.now)
        self.assertFalse(await self.repository.complete_admin_notice('new', now=self.now))
        self.assertIsNone(await self.repository.next_admin_notice(now=self.now))
        await self.accept('later', actor_id=2, now=self.now + timedelta(seconds=1))
        self.assertEqual((await self.repository.next_admin_notice(self.now + timedelta(seconds=2)))['job_id'], 'later')
        self.assertFalse(await self.repository.claim_admin_notice_recipient('new', 99, now=retry_at))
        self.assertTrue(await self.repository.claim_admin_notice_recipient('new', 100, now=retry_at))

    async def test_concurrent_finalization_cannot_erase_a_definite_rejection_retry(self):
        await self.initialize(recipients=(99,))
        await self.repository.claim_admin_notice_recipient('new', 99, now=self.now)
        original_find = self.repository._find_entry
        retry_at = self.now + timedelta(minutes=1)

        async def read_then_reject(job_id):
            snapshot = await original_find(job_id)
            await self.repository.update_admin_notice_recipient(job_id, 99, state='pending', retry_at=retry_at, now=self.now)
            return snapshot

        with patch.object(self.repository, '_find_entry', side_effect=read_then_reject):
            self.assertFalse(await self.repository.complete_admin_notice('new', now=self.now))
        self.assertEqual(self.entry('new')['admin_notice']['state'], 'pending')
        self.assertEqual(self.entry('new')['admin_notice']['recipients']['99']['state'], 'pending')
        await self.repository.complete_admin_notice('new', now=self.now)
        self.assertEqual(self.entry('new')['admin_notice']['next_attempt_at'], retry_at)

    async def test_definite_rejection_after_another_consumer_finalized_reopens_only_that_recipient(self):
        await self.initialize(recipients=(99,))
        await self.repository.claim_admin_notice_recipient('new', 99, now=self.now)
        self.assertTrue(await self.repository.complete_admin_notice('new', now=self.now))
        retry_at = self.now + timedelta(minutes=1)
        self.assertTrue(await self.repository.update_admin_notice_recipient(
            'new', 99, state='pending', retry_at=retry_at, now=self.now,
        ))
        await self.repository.complete_admin_notice('new', now=self.now)
        self.assertIsNone(await self.repository.next_admin_notice(now=self.now))
        self.assertEqual((await self.repository.next_admin_notice(now=retry_at))['job_id'], 'new')

    async def test_no_recipients_and_revoked_roles_finish_without_sending(self):
        await self.initialize(recipients=())
        self.assertTrue(await self.repository.complete_admin_notice('new', now=self.now))
        await self.initialize('revoked', recipients=(99,))
        self.assertTrue(await self.repository.skip_admin_notice_recipient('revoked', 99, now=self.now))
        self.assertFalse(await self.repository.claim_admin_notice_recipient('revoked', 99, now=self.now))
        self.assertTrue(await self.repository.complete_admin_notice('revoked', now=self.now))

    async def test_failed_delivery_keeps_error_bounded_and_acceptance_immutable(self):
        await self.initialize(recipients=(99,))
        original = copy.deepcopy(self.entry('new')['document'])
        await self.repository.claim_admin_notice_recipient('new', 99, now=self.now)
        await self.repository.update_admin_notice_recipient('new', 99, state='failed',
                                                          error='x' * (config.bot.error_max_length + 1), now=self.now)
        self.assertFalse(await self.repository.update_admin_notice_recipient('new', 99, state='sent', message_id=20, now=self.now))
        self.assertEqual(len(self.entry('new')['admin_notice']['recipients']['99']['error']), config.bot.error_max_length)
        self.assertEqual(self.entry('new')['document'], original)
        self.assertTrue(await self.repository.complete_admin_notice('new', now=self.now))

    async def test_recipient_identity_and_retry_arguments_cannot_inject_paths_or_reopen_unknown_send(self):
        await self.initialize(recipients=(99,))
        for identity in (True, 0, -1, '99', '99.state', 1.5):
            with self.subTest(identity=identity), self.assertRaises(ValueError):
                await self.repository.claim_admin_notice_recipient('new', identity, now=self.now)
        with self.assertRaises(ValueError):
            await self.repository.update_admin_notice_recipient('new', 99, state='pending', now=self.now)
        with self.assertRaises(ValueError):
            await self.repository.update_admin_notice_recipient('new', 99, state='sent', retry_at=self.now, now=self.now)
        self.assertEqual(self.entry('new')['admin_notice']['recipients']['99']['state'], 'pending')

    async def test_index_supports_new_notice_scan_without_expiring_quota_receipts(self):
        await self.repository.initialize_indexes()
        self.assertTrue(any(
            args[0] == [('admissions.state', 1), ('admissions.admin_notice.state', 1),
                        ('admissions.admin_notice.next_attempt_at', 1)]
            for args, kwargs in self.collection.indexes
        ))
        self.assertTrue(all('expireAfterSeconds' not in kwargs for args, kwargs in self.collection.indexes))
