"""Support messages are isolated by bot and recipient and reserve one send attempt."""
import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest

from bson import BSON
from bson.codec_options import CodecOptions
from bson.int64 import Int64

from src.support.config import support_config
from src.support.repository import SupportQueueFull, SupportRateLimited, SupportRepository


MISSING = object()


def value_at(row, path):
    for item in path.split('.'):
        if not isinstance(row, dict) or item not in row:
            return MISSING
        row = row[item]
    return row


def assign(row, path, value):
    parts = path.split('.')
    for item in parts[:-1]:
        row = row.setdefault(item, {})
    row[parts[-1]] = deepcopy(value)


def matches(row, query):
    for field, condition in query.items():
        if field == '$or':
            if not any(matches(row, branch) for branch in condition):
                return False
            continue
        value = value_at(row, field)
        if isinstance(condition, dict):
            for operation, operand in condition.items():
                if operation == '$gt' and (value is MISSING or value <= operand):
                    return False
                if operation == '$lte' and (value is MISSING or value > operand):
                    return False
                if operation == '$gte' and (value is MISSING or value < operand):
                    return False
                if operation == '$lt' and (value is MISSING or value >= operand):
                    return False
                if operation == '$in' and value not in operand:
                    return False
        elif value != condition:
            return False
    return True


class Cursor:
    def __init__(self, rows):
        self.rows = deepcopy(rows)

    async def to_list(self, length=None):
        return self.rows if length is None else self.rows[:length]


class Collection:
    """Atomic document model with ordinary dotted-field Mongo operations."""
    def __init__(self):
        self.rows, self.indexes, self.lock = {}, [], asyncio.Lock()

    def with_options(self, **options):
        self.write_concern = options['write_concern'].document
        return self

    async def create_index(self, *args, **kwargs):
        self.indexes.append((args, kwargs))

    async def find_one(self, query, projection=None, sort=None):
        rows = [row for row in self.rows.values() if matches(row, query)]
        for field, direction in reversed(sort or []):
            rows.sort(key=lambda row: value_at(row, field), reverse=direction < 0)
        return deepcopy(rows[0] if rows else None)

    def find(self, query, projection=None):
        return Cursor([row for row in self.rows.values() if matches(row, query)])

    async def count_documents(self, query):
        return sum(matches(row, query) for row in self.rows.values())

    async def update_one(self, query, update, upsert=False):
        async with self.lock:
            row = next((row for row in self.rows.values() if matches(row, query)), None)
            inserted = None
            if row is None and upsert:
                row = deepcopy(update.get('$setOnInsert', {}))
                row.setdefault('_id', query['_id'])
                inserted = row['_id']
                self.rows[inserted] = row
            if row is not None:
                for field, value in update.get('$set', {}).items():
                    assign(row, field, value)
            return SimpleNamespace(matched_count=int(row is not None), upserted_id=inserted)

    async def aggregate(self, pipeline):
        rows = deepcopy(list(self.rows.values()))
        for stage in pipeline:
            if '$match' in stage:
                rows = [row for row in rows if matches(row, stage['$match'])]
            elif '$set' in stage or '$project' in stage:
                operation = stage.get('$set', stage.get('$project'))
                for row in rows:
                    original = deepcopy(row)
                    if '$project' in stage:
                        row.clear()
                        row['_id'] = original['_id']
                    for field, expression in operation.items():
                        if isinstance(expression, dict) and '$objectToArray' in expression:
                            data = value_at(original, expression['$objectToArray'].removeprefix('$'))
                            row[field] = [{'k': key, 'v': deepcopy(value)} for key, value in data.items()]
            elif '$unwind' in stage:
                name = stage['$unwind'].removeprefix('$')
                expanded = []
                for row in rows:
                    for item in row[name]:
                        copy = deepcopy(row)
                        copy[name] = item
                        expanded.append(copy)
                rows = expanded
            elif '$sort' in stage:
                for field, direction in reversed(list(stage['$sort'].items())):
                    rows.sort(key=lambda row: value_at(row, field), reverse=direction < 0)
            elif '$limit' in stage:
                rows = rows[:stage['$limit']]
            elif '$group' in stage:
                rows = [{'_id': identity} for identity in dict.fromkeys(row['_id'] for row in rows)]
            elif '$count' in stage:
                rows = [{stage['$count']: len(rows)}] if rows else []
            else:
                raise AssertionError(stage)
        return Cursor(rows)


def payload(identifier=1, user=7):
    return {'chat_id': user, 'user_id': user, 'message_id': identifier,
            'name': 'Анна', 'username': 'anna', 'text': 'Нужна помощь', 'content_type': 'text'}


class SupportRepositoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.collection = Collection()
        self.repo = SupportRepository(self.collection, bot_id=100)

    async def test_journaled_idempotent_acceptance_has_separate_recipient_states_and_frozen_payload(self):
        original = payload()
        self.assertTrue(await self.repo.accept_user_message(original, [99, 99, 101]))
        original['text'] = 'changed later'
        self.assertFalse(await self.repo.accept_user_message(payload(), [99, 101]))
        candidate = await self.repo.next_delivery('support_copy')
        self.assertEqual(candidate['event']['payload']['text'], 'Нужна помощь')
        self.assertEqual(set(candidate['event']['deliveries']), {'99', '101'})
        self.assertEqual(set(candidate['event']['deliveries']['99']), {'support_copy'})
        self.assertEqual(self.collection.write_concern, {'w': 1, 'j': True})
        self.assertEqual(await self.repo.pending_count(7), 1)

    async def test_concurrent_reservations_allow_one_attempt_and_unknown_outcome_is_not_retried(self):
        await self.repo.accept_user_message(payload(), [99])
        event = (await self.repo.next_delivery('support_copy'))['event_id']
        results = await asyncio.gather(*[self.repo.reserve_delivery(event, 99, 'support_copy') for _ in range(12)])
        self.assertEqual(sum(results), 1)
        self.assertIsNone(await self.repo.next_delivery('support_copy'))
        self.assertFalse(await self.repo.reserve_delivery(event, 99, 'support_copy'))

    async def test_only_explicit_retry_time_can_return_reserved_delivery_to_pending(self):
        await self.repo.accept_user_message(payload(), [99])
        candidate = await self.repo.next_delivery('support_copy')
        key = candidate['event_id']
        await self.repo.reserve_delivery(key, 99, 'support_copy')
        with self.assertRaises(ValueError):
            await self.repo.finish_delivery(key, 99, 'support_copy', state='pending')
        retry = datetime.now(timezone.utc) + timedelta(minutes=1)
        self.assertTrue(await self.repo.finish_delivery(key, 99, 'support_copy', state='pending', retry_at=retry))
        self.assertIsNone(await self.repo.next_delivery('support_copy'))
        self.assertIsNotNone(await self.repo.next_delivery('support_copy', now=retry))

    async def test_unactivated_admin_does_not_block_an_activated_admin_or_other_channel(self):
        await self.repo.accept_user_message(payload(), [98, 99])
        self.assertIsNone(await self.repo.next_delivery('support_copy', active_admin_ids=[]))
        active = await self.repo.next_delivery('support_copy', active_admin_ids=[99])
        self.assertEqual(active['recipient_id'], 99)
        with self.assertRaises(ValueError):
            await self.repo.next_delivery('main_alert')
        await self.repo.activate_admin(99)
        self.assertEqual(await self.repo.active_admin_ids(), [99])
        marker = next(row for row in self.collection.rows.values() if row['kind'] == 'activation')
        self.assertNotIn('expires_at', marker)

    async def test_bindings_are_scoped_to_bot_and_admin_and_replies_use_original_user(self):
        await self.repo.accept_user_message(payload(), [99])
        event = (await self.repo.next_delivery('support_copy', [99]))['event_id']
        await self.repo.bind_admin_message(99, 40, 7, 1, event)
        target = await self.repo.get_reply_target(99, 40)
        self.assertEqual(target['user_chat_id'], 7)
        self.assertIsNone(await self.repo.get_reply_target(98, 40))
        other_bot = SupportRepository(self.collection, bot_id=101)
        self.assertIsNone(await other_bot.get_reply_target(99, 40))
        self.assertTrue(await self.repo.accept_admin_reply(payload(41, user=99), target))
        self.assertFalse(await self.repo.accept_admin_reply(payload(41, user=99), target))
        outgoing = await self.repo.next_delivery('outbound_reply')
        self.assertEqual((outgoing['recipient_id'], outgoing['event']['admin_id']), (7, 99))
        with self.assertRaises(ValueError):
            await self.repo.bind_admin_message(99, 40, 8, 2, event)

    async def test_capacity_is_atomic_within_process_and_duplicate_is_accepted_before_limit_check(self):
        self.repo.policy = replace(support_config, max_pending_per_user=1, max_pending_total=2)
        first = await asyncio.gather(*[self.repo.accept_user_message(payload(index), [99])
                                      for index in (1, 2)], return_exceptions=True)
        self.assertEqual(sum(item is True for item in first), 1)
        self.assertEqual(sum(isinstance(item, SupportQueueFull) for item in first), 1)
        accepted_id = next(row['source_message_id'] for row in self.collection.rows.values())
        self.assertFalse(await self.repo.accept_user_message(payload(accepted_id), [99]))
        self.assertTrue(await self.repo.accept_user_message(payload(1, user=8), [99]))
        with self.assertRaises(SupportQueueFull):
            await self.repo.accept_user_message(payload(1, user=9), [99])

    async def test_expired_events_and_mappings_are_not_used_before_ttl_cleanup(self):
        old = datetime.now(timezone.utc) - timedelta(seconds=support_config.retention_seconds + 1)
        await self.repo.accept_user_message(payload(), [99], now=old)
        self.assertIsNone(await self.repo.next_delivery('support_copy'))
        self.assertEqual(await self.repo.pending_count(), 0)
        self.assertFalse(await self.repo.reserve_delivery('in:100:7:1', 99, 'support_copy'))
        self.assertIsNone(await self.repo.get_event('in:100:7:1'))
        await self.repo.bind_admin_message(99, 40, 7, 1, 'in:100:7:1', now=old)
        self.assertIsNone(await self.repo.get_reply_target(99, 40))

    async def test_archive_limit_counts_only_this_bots_events_and_keeps_duplicate_replies_idempotent(self):
        self.repo.policy = replace(support_config, max_stored_events=2,
                                   max_pending_total=1, max_pending_per_user=1)
        await self.repo.accept_user_message(payload(), [99])
        await self.repo.activate_admin(99)
        await self.repo.bind_admin_message(99, 40, 7, 1, 'in:100:7:1')
        target = await self.repo.get_reply_target(99, 40)
        await self.repo.accept_admin_reply(payload(41, user=99), target)
        self.assertEqual(await self.repo.stored_count(), 2)
        self.assertFalse(await self.repo.accept_admin_reply(payload(41, user=99), target))
        with self.assertRaises(SupportQueueFull):
            await self.repo.accept_user_message(payload(2), [99])
        other = SupportRepository(self.collection, bot_id=101)
        self.assertEqual(await other.stored_count(), 0)

    async def test_payload_and_path_id_validation_happens_before_write(self):
        for invalid in (dict(payload(), user_id=8), dict(payload(), extra='secret'),
                        dict(payload(), text='x' * (support_config.max_text_length + 1))):
            with self.subTest(invalid=invalid.keys()), self.assertRaises(ValueError):
                await self.repo.accept_user_message(invalid, [99])
        with self.assertRaises(ValueError):
            await self.repo.reserve_delivery('in:100:7:1', '99.bad', 'support_copy')
        self.assertEqual(self.collection.rows, {})

    async def test_large_bot_user_and_admin_ids_survive_real_bson_roundtrips(self):
        bot_id, user_id, admin_id = (Int64(value) for value in (9000000001, 6000000000, 6000000001))
        repo = SupportRepository(self.collection, bot_id=bot_id)
        self.assertIs(type(repo.bot_id), int)
        await repo.accept_user_message(payload(user=user_id), [admin_id])
        await repo.activate_admin(admin_id)

        def bson_roundtrip():
            self.collection.rows = {
                key: BSON.encode(row).decode(CodecOptions(tz_aware=True))
                for key, row in self.collection.rows.items()
            }

        bson_roundtrip()
        activated = await repo.active_admin_ids()
        self.assertEqual(activated, [int(admin_id)])
        self.assertIs(type(activated[0]), int)
        candidate = await repo.next_delivery('support_copy', active_admin_ids=activated)
        event = candidate['event']
        self.assertIs(type(event['user_chat_id']), Int64)
        await repo.bind_admin_message(candidate['recipient_id'], 40, event['user_chat_id'],
                                      event['source_message_id'], candidate['event_id'])
        bson_roundtrip()
        target = await repo.get_reply_target(admin_id, 40)
        self.assertEqual(target['user_chat_id'], int(user_id))
        self.assertIs(type(target['user_chat_id']), int)
        self.assertTrue(await repo.accept_admin_reply(payload(41, user=admin_id), target))
        bson_roundtrip()
        outgoing = await repo.next_delivery('outbound_reply')
        self.assertEqual(outgoing['recipient_id'], int(user_id))
        self.assertTrue(await repo.reserve_delivery(outgoing['event_id'], user_id, 'outbound_reply'))
        for bad in (True, False, 1.5, '6000000000'):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                SupportRepository(self.collection, bot_id=bad)

    async def test_known_forward_receipt_routes_replies_when_mapping_projection_is_missing(self):
        user_id, admin_id = 6000000000, 6000000001
        await self.repo.accept_user_message(payload(user=user_id), [admin_id])
        candidate = await self.repo.next_delivery('support_copy', active_admin_ids=[admin_id])
        self.assertTrue(await self.repo.reserve_delivery(candidate['event_id'], admin_id, 'support_copy'))
        self.assertTrue(await self.repo.finish_delivery(candidate['event_id'], admin_id, 'support_copy',
                                                       state='sent', message_id=40))
        self.collection.rows = {key: BSON.encode(row).decode(CodecOptions(tz_aware=True))
                                for key, row in self.collection.rows.items()}
        self.assertFalse(any(row['kind'] == 'mapping' for row in self.collection.rows.values()))
        target = await self.repo.get_reply_target(admin_id, 40)
        self.assertEqual(target, {'user_chat_id': user_id, 'source_message_id': 1,
                                  'event_id': candidate['event_id']})
        self.assertIs(type(target['user_chat_id']), int)
        self.assertIsNone(await self.repo.get_reply_target(admin_id + 1, 40))
        self.assertIsNone(await SupportRepository(self.collection, bot_id=101).get_reply_target(admin_id, 40))
        row = self.collection.rows[candidate['event_id']]
        row['expires_at'] = datetime.now(timezone.utc) - timedelta(seconds=1)
        self.assertIsNone(await self.repo.get_reply_target(admin_id, 40))


class DurableSupportRateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.collection = Collection()
        self.repo = SupportRepository(self.collection, bot_id=100)
        self.now = datetime(2026, 11, 22, 9, tzinfo=timezone.utc)

    async def accepted_and_delivered(self, identifier, moment):
        self.assertTrue(await self.repo.accept_user_message(payload(identifier), [99], now=moment))
        self.collection.rows[f'in:100:7:{identifier}']['deliveries']['99']['support_copy']['state'] = 'sent'

    async def test_parallel_new_messages_admit_one_then_restart_cannot_reset_cooldown(self):
        results = await asyncio.gather(*[
            self.repo.accept_user_message(payload(identifier), [99], now=self.now)
            for identifier in range(1, 13)
        ], return_exceptions=True)
        self.assertEqual(sum(item is True for item in results), 1)
        self.assertEqual(sum(isinstance(item, SupportRateLimited) for item in results), 11)
        replacement = SupportRepository(self.collection, bot_id=100)
        with self.assertRaises(SupportRateLimited) as caught:
            await replacement.accept_user_message(payload(20), [99], now=self.now + timedelta(seconds=9))
        self.assertEqual(caught.exception.retry_after_seconds, 1)
        self.assertTrue(await replacement.accept_user_message(payload(21), [99], now=self.now + timedelta(seconds=10)))

    async def test_delivered_messages_still_count_toward_rolling_window_and_duplicate_does_not(self):
        for identifier in range(1, 6):
            await self.accepted_and_delivered(identifier, self.now + timedelta(seconds=(identifier - 1) * 10))
        self.assertFalse(await self.repo.accept_user_message(payload(1), [99], now=self.now + timedelta(seconds=50)))
        with self.assertRaises(SupportRateLimited) as caught:
            await self.repo.accept_user_message(payload(6), [99], now=self.now + timedelta(seconds=50))
        self.assertEqual(caught.exception.retry_after_seconds, 550)
        replacement = SupportRepository(self.collection, bot_id=100)
        self.assertTrue(await replacement.accept_user_message(payload(6), [99], now=self.now + timedelta(seconds=600)))
        self.assertEqual(sum(row['kind'] == 'incoming' for row in self.collection.rows.values()), 6)

    async def test_moscow_daily_limit_survives_delivery_restart_and_resets_at_local_midnight(self):
        for identifier in range(1, 21):
            await self.accepted_and_delivered(identifier, self.now + timedelta(seconds=(identifier - 1) * 601))
        replacement = SupportRepository(self.collection, bot_id=100)
        with self.assertRaises(SupportRateLimited) as caught:
            await replacement.accept_user_message(payload(21), [99], now=self.now + timedelta(seconds=20 * 601))
        midnight = datetime(2026, 11, 22, 21, tzinfo=timezone.utc)
        self.assertEqual(caught.exception.retry_at, midnight)
        self.assertTrue(await replacement.accept_user_message(payload(21), [99], now=midnight))

    async def test_capacity_rejection_and_other_actors_do_not_consume_this_users_rate(self):
        repo = SupportRepository(self.collection, bot_id=100,
                                 policy=replace(support_config, max_pending_per_user=1))
        self.assertTrue(await repo.accept_user_message(payload(1), [99], now=self.now))
        due = self.now + timedelta(seconds=10)
        with self.assertRaises(SupportQueueFull):
            await repo.accept_user_message(payload(2), [99], now=due)
        self.collection.rows['in:100:7:1']['deliveries']['99']['support_copy']['state'] = 'sent'
        self.assertTrue(await repo.accept_user_message(payload(2), [99], now=due))
        self.assertTrue(await repo.accept_user_message(payload(1, user=8), [99], now=due))
        self.assertEqual(sum(row['kind'] == 'incoming' for row in self.collection.rows.values()), 3)

    async def test_bson_history_and_legacy_main_alert_do_not_create_a_main_delivery(self):
        await self.accepted_and_delivered(1, self.now)
        self.collection.rows['in:100:7:1']['deliveries']['99']['main_alert'] = {'state': 'pending', 'next_attempt_at': self.now}
        self.collection.rows = {key: BSON.encode(row).decode(CodecOptions(tz_aware=True))
                                for key, row in self.collection.rows.items()}
        self.assertEqual(await self.repo.pending_count(7), 0)
        with self.assertRaises(ValueError):
            await self.repo.next_delivery('main_alert')
        with self.assertRaises(SupportRateLimited):
            await SupportRepository(self.collection, bot_id=100).accept_user_message(payload(2), [99], now=self.now)
