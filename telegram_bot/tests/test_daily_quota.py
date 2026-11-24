import asyncio
import copy
import hashlib
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from pymongo.errors import DuplicateKeyError, NetworkTimeout
from pymongo import ReturnDocument

from imager_contracts import make_submission
from imager_config.loader import config
from src.application.admission import AdmissionDispatcher
from src.infrastructure.data.errors import AdmissionConflict, DailyQuotaExceeded
from src.infrastructure.data.repositories.admission_repository import AdmissionRepository, DailyAdmissionLimitExceeded


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


_MISSING = object()


def values_at(document, path):
    parts = path.split('.') if isinstance(path, str) else path
    if not parts:
        return [document]
    if isinstance(document, list):
        if parts[0].isdigit():
            index = int(parts[0])
            return values_at(document[index], parts[1:]) if index < len(document) else [_MISSING]
        return [value for row in document for value in values_at(row, parts)]
    if not isinstance(document, dict) or parts[0] not in document:
        return [_MISSING]
    return values_at(document[parts[0]], parts[1:])


def matches(document, query):
    for field, condition in query.items():
        if field in ('$or', '$nor'):
            found = any(matches(document, branch) for branch in condition)
            if found != (field == '$or'):
                return False
            continue
        values = values_at(document, field)
        if isinstance(condition, dict) and any(key.startswith('$') for key in condition):
            existing = [value for value in values if value is not _MISSING]
            for operator, operand in condition.items():
                if operator == '$elemMatch':
                    valid = any(isinstance(value, list) and any(matches(row, operand) for row in value)
                                for value in values)
                elif operator == '$ne':
                    valid = not any(value == operand for value in values)
                elif operator == '$lt':
                    valid = any(value < operand for value in existing)
                elif operator == '$lte':
                    valid = any(value <= operand for value in existing)
                elif operator == '$gt':
                    valid = any(value > operand for value in existing)
                elif operator == '$exists':
                    valid = bool(existing) == operand
                else:
                    raise AssertionError(f'Unsupported model operator: {operator}')
                if not valid:
                    return False
        elif condition is None:
            if not any(value is None or value is _MISSING for value in values):
                return False
        elif not any(value == condition for value in values):
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

    async def index_information(self):
        result = {}
        for args, options in self.indexes:
            key = [(args[0], 1)] if isinstance(args[0], str) else list(args[0])
            name = options.get('name') or '_'.join(f'{field}_{direction}' for field, direction in key)
            result[name] = dict(options, key=key)
        return result

    async def drop_index(self, name):
        info = await self.index_information()
        self.indexes = [(args, options) for args, options in self.indexes
                        if options.get('name') != name]
        assert name in info

    async def update_many(self, query, update):
        count = 0
        async with self.lock:
            for document in self.documents.values():
                if matches(document, query):
                    self._apply(document, query, update)
                    count += 1
        return SimpleNamespace(matched_count=count, modified_count=count)

    async def find_one(self, query, projection=None, sort=None):
        async with self.lock:
            values = [doc for doc in self.documents.values() if matches(doc, query)]
            for field, direction in reversed(sort or []):
                values.sort(key=lambda doc: (doc.get(field) is not None, doc.get(field)),
                            reverse=direction < 0)
            return copy.deepcopy(values[0] if values else None)

    async def count_documents(self, query):
        async with self.lock:
            return sum(matches(doc, query) for doc in self.documents.values())

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
                elif operation == '$unset':
                    target.pop(field, None)

    async def update_one(self, query, update, upsert=False):
        async with self.lock:
            document = next((doc for doc in self.documents.values() if matches(doc, query)), None)
            if document is None and upsert:
                document = copy.deepcopy(update['$setOnInsert'])
                self.documents[document['_id']] = document
            if document is not None:
                self._apply(document, query, update)
            return SimpleNamespace(matched_count=int(document is not None))

    async def find_one_and_update(self, query, update, *, upsert=False,
                                  return_document=ReturnDocument.AFTER, **kwargs):
        async with self.lock:
            original = next((doc for doc in self.documents.values() if matches(doc, query)), None)
            if original is None and not upsert:
                return None
            candidate = copy.deepcopy(original if original is not None else update['$setOnInsert'])
            if original is None:
                for entry in candidate.get('admissions', []):
                    if any(current['job_id'] == entry['job_id']
                           for bucket in self.documents.values() for current in bucket['admissions']):
                        raise DuplicateKeyError('job identity')
            self._apply(candidate, query, update)
            self.documents[candidate['_id']] = candidate
            if self.fail_after_write:
                self.fail_after_write = False
                raise NetworkTimeout('ack lost after commit')
            return copy.deepcopy(candidate if return_document == ReturnDocument.AFTER else original)

    async def aggregate(self, pipeline):
        if any('$unwind' in stage for stage in pipeline):
            # Evaluate the claim's entry-level match and ordering, rather than
            # the surrounding daily bucket's insertion/creation order.
            condition = next(stage['$match']['$or'] for stage in pipeline
                             if '$match' in stage and '$or' in stage['$match'])
            eligible = {'$or': [
                {key.removeprefix('admissions.'): value for key, value in item.items()}
                for item in condition
            ]}
            candidates = [
                (bucket['_id'], entry)
                for bucket in self.documents.values() for entry in bucket['admissions']
                if matches(entry, eligible)
            ]
            ordering = next(stage['$sort'] for stage in pipeline if '$sort' in stage)
            for field, direction in reversed(list(ordering.items())):
                candidates.sort(
                    key=lambda item: item[1][field.removeprefix('admissions.')],
                    reverse=direction < 0,
                )
            limit = next(stage['$limit'] for stage in pipeline if '$limit' in stage)
            result = [{'_id': bucket_id, 'job_id': entry['job_id']}
                      for bucket_id, entry in candidates[:limit]]
            return SimpleNamespace(to_list=AsyncMock(return_value=result))
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

    async def test_refunded_failures_keep_receipts_bounded_and_replayable(self):
        limit = config.bot.max_daily_admissions
        for index in range(limit):
            job_id = f'failed-{index}'
            await self.repository.accept_submission(submission(job_id), actor_id=1, now=self.now)
            self.assertTrue(await self.repository.refund(job_id, now=self.now))
            self.assertFalse(await self.repository.refund(job_id, now=self.now))
        bucket = self.collection.documents['1:2026-11-12']
        self.assertEqual((bucket['used'], len(bucket['admissions'])), (0, limit))
        self.assertEqual(await self.repository.remaining(1, self.now), 5)
        with self.assertRaises(DailyAdmissionLimitExceeded):
            await self.repository.ensure_admission_capacity(1, now=self.now)
        with self.assertRaises(DailyAdmissionLimitExceeded):
            await self.repository.accept_submission(submission('excess'), actor_id=1, now=self.now)
        replay = await self.repository.accept_submission(submission('failed-0'), actor_id=1, now=self.now)
        self.assertTrue(replay.duplicate)
        self.assertEqual(len(bucket['admissions']), limit)
        # A new calendar day has its own attempt and successful-image budgets.
        tomorrow = self.now + timedelta(days=1)
        await self.repository.ensure_admission_capacity(1, now=tomorrow)
        await self.repository.accept_submission(submission('tomorrow'), actor_id=1, now=tomorrow)
        self.assertEqual(await self.repository.remaining(1, tomorrow), 4)

    async def test_source_pin_matches_bucket_and_key_in_the_same_admission(self):
        first, second = submission('first'), submission('second')
        await self.repository.accept_submission(first, actor_id=1, now=self.now)
        await self.repository.accept_submission(second, actor_id=1, now=self.now)
        self.assertTrue(await self.repository.source_is_referenced('imager', 'uploads/first/source.jpg'))
        self.assertFalse(await self.repository.source_is_referenced('imager', 'uploads/missing/source.jpg'))
        # Even malformed legacy data must not combine a bucket from one entry
        # with the key from another when checking whether an object is pinned.
        bucket = self.collection.documents['1:2026-11-12']
        bucket['admissions'][0]['document']['request']['source']['bucket'] = 'other-bucket'
        self.assertFalse(await self.repository.source_is_referenced('imager', 'uploads/first/source.jpg'))

    async def test_last_attempt_slot_is_atomic_even_when_the_success_quota_is_free(self):
        for index in range(config.bot.max_daily_admissions - 1):
            job_id = f'failed-{index}'
            await self.repository.accept_submission(submission(job_id), actor_id=1, now=self.now)
            await self.repository.refund(job_id, now=self.now)
        results = await asyncio.gather(*[
            self.repository.accept_submission(submission(f'last-{index}'), actor_id=1, now=self.now)
            for index in range(2)
        ], return_exceptions=True)
        self.assertEqual(sum(isinstance(result, DailyAdmissionLimitExceeded) for result in results), 1)
        self.assertEqual(await self.repository.remaining(1, self.now), 4)
        bucket = self.collection.documents['1:2026-11-12']
        self.assertEqual(len(bucket['admissions']), config.bot.max_daily_admissions)

    async def test_legacy_oversized_bucket_is_preserved_but_does_not_grow(self):
        await self.repository.accept_submission(submission('legacy'), actor_id=1, now=self.now)
        bucket = self.collection.documents['1:2026-11-12']
        template = copy.deepcopy(bucket['admissions'][0])
        for index in range(config.bot.max_daily_admissions):
            entry = copy.deepcopy(template)
            document = submission(f'legacy-{index}')
            entry.update(job_id=document['_id'], document=document, payload_hash=document['payload_hash'])
            bucket['admissions'].append(entry)
        bucket['used'] = 0
        original = copy.deepcopy(bucket)
        with self.assertRaises(DailyAdmissionLimitExceeded):
            await self.repository.accept_submission(submission('new'), actor_id=1, now=self.now)
        self.assertTrue((await self.repository.accept_submission(submission('legacy'), actor_id=1, now=self.now)).duplicate)
        self.assertEqual(bucket, original)

    async def test_administrator_attempts_remain_unlimited_and_refund_survives_demotion(self):
        for index in range(config.bot.max_daily_admissions + 1):
            job_id = f'admin-{index}'
            await self.repository.accept_submission(submission(job_id), actor_id=1,
                                                    now=self.now, quota_exempt=True)
            self.assertTrue(await self.repository.refund(job_id, now=self.now))
        await self.repository.ensure_admission_capacity(1, now=self.now)
        self.assertEqual(await self.repository.remaining(1, self.now), 5)
        self.assertTrue(all(len(bucket['admissions']) <= 1 for bucket in self.collection.documents.values()))
        await self.repository.accept_submission(submission('demoted'), actor_id=1, now=self.now)
        self.assertEqual(await self.repository.remaining(1, self.now), 4)

    async def test_unlimited_administrators_use_one_bounded_bucket_per_image(self):
        receipts = await asyncio.gather(*[
            self.repository.accept_submission(submission(f'admin-{index}'), actor_id=1,
                                              now=self.now, quota_exempt=True)
            for index in range(20)
        ])
        self.assertTrue(all(not receipt.duplicate for receipt in receipts))
        self.assertEqual(await self.repository.pending_count(), 20)
        exempt = [row for row in self.collection.documents.values() if row.get('quota_exempt')]
        self.assertEqual(len(exempt), 20)
        self.assertTrue(all(len(row['admissions']) == 1 and row['used'] == 1 for row in exempt))
        normal = self.collection.documents['1:2026-11-12']
        self.assertEqual((normal['used'], normal['admissions']), (0, []))
        # This value is the ordinary quota after a possible role revocation.
        self.assertEqual(await self.repository.remaining(1, self.now), 0)

    async def test_concurrent_duplicate_administrator_photo_has_one_receipt(self):
        receipts = await asyncio.gather(*[
            self.repository.accept_submission(submission('same-admin'), actor_id=1,
                                              now=self.now, quota_exempt=True)
            for _ in range(4)
        ])
        self.assertEqual(sum(not receipt.duplicate for receipt in receipts), 1)
        self.assertEqual(sum(len(row['admissions']) for row in self.collection.documents.values()), 1)

    async def test_demotion_and_promotion_keep_existing_daily_usage(self):
        await self.repository.ensure_day(1, initial_used=1, now=self.now)
        for index in range(2):
            await self.repository.accept_submission(submission(f'admin-{index}'), actor_id=1,
                                                    now=self.now, quota_exempt=True)
        self.assertEqual(await self.repository.remaining(1, self.now), 2)
        for index in range(2):
            await self.repository.accept_submission(submission(f'regular-{index}'), actor_id=1, now=self.now)
        with self.assertRaises(DailyQuotaExceeded):
            await self.repository.accept_submission(submission('sixth'), actor_id=1, now=self.now)
        promoted = await self.repository.accept_submission(submission('promoted'), actor_id=1,
                                                          now=self.now, quota_exempt=True)
        self.assertFalse(promoted.duplicate)
        self.assertEqual(await self.repository.remaining(1, self.now), 0)

    async def test_admin_lost_ack_reconciles_without_second_document(self):
        await self.repository.ensure_day(1, now=self.now)
        self.collection.fail_after_write = True
        receipt = await self.repository.accept_submission(submission('admin'), actor_id=1,
                                                          now=self.now, quota_exempt=True)
        self.assertTrue(receipt.duplicate)
        self.assertEqual(await self.repository.pending_count(), 1)

    async def test_admin_receipt_replay_on_next_day_does_not_create_another_bucket(self):
        first = await self.repository.accept_submission(submission('admin'), actor_id=1,
                                                        now=self.now, quota_exempt=True)
        replay = await self.repository.accept_submission(submission('admin'), actor_id=1,
                                                         now=self.now + timedelta(days=1), quota_exempt=True)
        self.assertTrue(replay.duplicate)
        self.assertEqual(replay.date, first.date)
        self.assertEqual(sum(bool(row.get('quota_exempt')) for row in self.collection.documents.values()), 1)

    async def test_latest_receipt_follows_acceptance_time_across_admin_and_regular_buckets(self):
        await self.repository.accept_submission(submission('first'), actor_id=1, now=self.now)
        await self.repository.accept_submission(submission('admin'), actor_id=1,
                                                now=self.now + timedelta(minutes=1), quota_exempt=True)
        self.assertEqual((await self.repository.latest(1)).job_id, 'admin')
        await self.repository.accept_submission(submission('latest'), actor_id=1,
                                                now=self.now + timedelta(minutes=2))
        self.assertEqual((await self.repository.latest(1)).job_id, 'latest')

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

    async def test_claim_orders_entries_across_accounts_instead_of_daily_buckets(self):
        early = self.now.replace(hour=9)
        older = self.now.replace(hour=10)
        newer = older + timedelta(minutes=1)
        await self.repository.accept_submission(submission('a-first'), actor_id=1, now=early)
        first = await self.repository.claim(early)
        await self.repository.acknowledge(first.job_id, first.token, now=early)
        # The account's 09:00 bucket must not promote its 10:01 image over
        # another account's 10:00 image, despite being inserted earlier.
        await self.repository.accept_submission(submission('b-older', user_id=2), actor_id=2, now=older)
        await self.repository.accept_submission(submission('a-newer'), actor_id=1, now=newer)
        selected = await self.repository.claim(newer)
        self.assertEqual(selected.job_id, 'b-older')
        selected = await self.repository.claim(newer)
        self.assertEqual(selected.job_id, 'a-newer')

    async def test_live_claim_and_deferred_retry_are_skipped_until_eligible(self):
        await self.repository.accept_submission(submission('first'), actor_id=1, now=self.now)
        await self.repository.accept_submission(submission('second', user_id=2), actor_id=2,
                                                now=self.now + timedelta(seconds=1))
        first = await self.repository.claim(self.now)
        second = await self.repository.claim(self.now + timedelta(seconds=2))
        self.assertEqual((first.job_id, second.job_id), ('first', 'second'))
        self.assertIsNone(await self.repository.claim(self.now + timedelta(seconds=59)))
        reclaimed = await self.repository.claim(self.now + timedelta(seconds=60))
        self.assertEqual(reclaimed.job_id, 'first')
        self.assertNotEqual(reclaimed.token, first.token)
        self.assertFalse(await self.repository.acknowledge(first.job_id, first.token))
        await self.repository.retry(reclaimed.job_id, reclaimed.token, RuntimeError('temporarily down'),
                                    error_max_length=100, now=self.now + timedelta(seconds=60))
        self.assertIsNone(await self.repository.claim(self.now + timedelta(seconds=61)))
        reclaimed_again = await self.repository.claim(self.now + timedelta(seconds=63))
        self.assertEqual(reclaimed_again.job_id, 'first')

    async def test_equal_acceptance_times_have_deterministic_job_id_order(self):
        await self.repository.accept_submission(submission('z-last'), actor_id=1, now=self.now)
        await self.repository.accept_submission(submission('a-first', user_id=2), actor_id=2, now=self.now)
        self.assertEqual((await self.repository.claim(self.now)).job_id, 'a-first')

    async def test_lost_candidate_claim_returns_without_claiming_another_entry(self):
        await self.repository.accept_submission(submission('first'), actor_id=1, now=self.now)
        await self.repository.accept_submission(submission('second', user_id=2), actor_id=2, now=self.now)
        original_update = self.collection.find_one_and_update
        updates = 0

        async def competing_claim(query, change, **kwargs):
            nonlocal updates
            updates += 1
            # Another dispatcher has renewed the selected entry after the
            # candidate read. The CAS must neither take it nor a sibling.
            selected_id = query['admissions']['$elemMatch']['job_id']
            for bucket in self.collection.documents.values():
                for entry in bucket['admissions']:
                    if entry['job_id'] == selected_id:
                        entry.update(state='claimed', token='other-owner',
                                     lease_until=self.now + timedelta(seconds=60))
            return await original_update(query, change, **kwargs)

        self.collection.find_one_and_update = competing_claim
        self.assertIsNone(await self.repository.claim(self.now))
        self.assertEqual(updates, 1)
        self.assertEqual((await self.repository.find('second')).state, 'pending')

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
