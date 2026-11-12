import copy
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError, OperationFailure

from src.infrastructure.data.repositories.generation_progress_repository import (
    GenerationProgressRepository,
)


class GenerationProgressRepositoryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.documents = {}
        self.collection = Mock()
        self.collection.create_index = AsyncMock()
        self.collection.find_one_and_update = AsyncMock(side_effect=self.upsert)
        self.collection.find_one = AsyncMock(side_effect=self.find_one)
        self.collection.update_one = AsyncMock(side_effect=self.update)
        self.repository = GenerationProgressRepository(self.collection, retention_seconds=3600)

    async def upsert(self, query, change, **kwargs):
        self.documents.setdefault(query['_id'], copy.deepcopy(change['$setOnInsert']))
        return copy.deepcopy(self.documents[query['_id']])

    async def find_one(self, query):
        return copy.deepcopy(self.documents.get(query['_id']))

    async def update(self, query, change):
        document = self.documents.get(query['_id'])
        if document is not None:
            document.update(copy.deepcopy(change['$set']))
        return SimpleNamespace(matched_count=int(document is not None))

    async def track(self, **changes):
        values = {'actor_id': 5, 'chat_id': 15, 'message_id': 25, 'text': 'В очереди: 2.'}
        values.update(changes)
        return await self.repository.track('image-1', **values)

    async def test_active_messages_have_no_ttl_and_recovery_index_has_stable_order(self):
        await self.repository.initialize_indexes()
        self.collection.create_index.assert_any_await('expires_at', expireAfterSeconds=0)
        self.collection.create_index.assert_any_await([
            ('finished', 1), ('created_at', 1), ('_id', 1),
        ])
        before = datetime.now(timezone.utc)
        stored = await self.track()
        self.assertEqual(stored['message_id'], 25)
        self.assertIs(stored['finished'], False)
        self.assertNotIn('expires_at', stored)
        self.assertLessEqual(before, stored['created_at'])
        self.assertLessEqual(stored['created_at'], datetime.now(timezone.utc))
        self.assertEqual(stored['created_at'], stored['updated_at'])
        self.assertIs(stored['created_at'].tzinfo, timezone.utc)
        _, kwargs = self.collection.find_one_and_update.call_args
        self.assertIs(kwargs['return_document'], ReturnDocument.AFTER)
        self.assertIs(kwargs['upsert'], True)

    async def test_duplicate_keeps_original_message_text_timestamps_and_terminal_state(self):
        await self.track()
        await self.repository.remember('image-1', 'Готово!', finished=True)
        original = copy.deepcopy(self.documents['image-1'])
        stored = await self.track(message_id=999, text='Новая копия')
        self.assertEqual(stored, original)
        self.assertEqual(self.documents['image-1'], original)
        self.assertEqual(len(self.documents), 1)

    async def test_duplicate_for_another_actor_or_chat_is_rejected(self):
        original = await self.track()
        for changes in ({'actor_id': 6}, {'chat_id': 16}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                await self.track(**changes)
        self.assertEqual(self.documents['image-1'], original)

    async def test_concurrent_insert_reuses_the_existing_message_and_checks_owner(self):
        original = await self.track()
        self.collection.find_one_and_update.side_effect = DuplicateKeyError('race')
        self.assertEqual(await self.track(message_id=999), original)
        self.collection.find_one.assert_awaited_with({'_id': 'image-1'})
        with self.assertRaises(ValueError):
            await self.track(actor_id=6)

    async def test_unrelated_database_failures_are_not_hidden(self):
        self.collection.find_one_and_update.side_effect = OperationFailure('unavailable')
        with self.assertRaises(OperationFailure):
            await self.track()
        self.collection.find_one_and_update.side_effect = DuplicateKeyError('another index')
        with self.assertRaises(DuplicateKeyError):
            await self.track()

    async def test_get_returns_only_the_requested_projection(self):
        original = await self.track()
        self.assertEqual(await self.repository.get('image-1'), original)
        self.assertIsNone(await self.repository.get('missing'))
        self.collection.find_one.assert_awaited_with({'_id': 'missing'})

    async def test_active_reads_a_bounded_async_cursor_in_stable_creation_order(self):
        records = [{'_id': 'image-1'}, {'_id': 'image-2'}]
        cursor = Mock()
        cursor.sort.return_value = cursor
        cursor.limit.return_value = cursor
        cursor.to_list = AsyncMock(return_value=records)
        self.collection.find.return_value = cursor
        self.assertEqual(await self.repository.active(2), records)
        self.collection.find.assert_called_once_with({'finished': False})
        cursor.sort.assert_called_once_with([('created_at', 1), ('_id', 1)])
        cursor.limit.assert_called_once_with(2)
        cursor.to_list.assert_awaited_once_with(length=2)

    async def test_invalid_batch_size_cannot_turn_recovery_into_an_unbounded_query(self):
        for limit in (0, -1, True, 1.5):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                await self.repository.active(limit)
        self.collection.find.assert_not_called()

    async def test_progress_remains_recoverable_until_finished_then_gets_retention_ttl(self):
        original = await self.track()
        self.assertTrue(await self.repository.remember('image-1', 'Собираю изображение…'))
        active = self.documents['image-1']
        self.assertEqual(active['last_text'], 'Собираю изображение…')
        self.assertEqual(active['created_at'], original['created_at'])
        self.assertIs(active['finished'], False)
        self.assertNotIn('expires_at', active)
        before = datetime.now(timezone.utc)
        self.assertTrue(await self.repository.remember('image-1', 'Готово!', finished=True))
        terminal = copy.deepcopy(self.documents['image-1'])
        self.assertIs(terminal['finished'], True)
        self.assertEqual(terminal['expires_at'] - terminal['updated_at'], timedelta(seconds=3600))
        self.assertLessEqual(before + timedelta(seconds=3600), terminal['expires_at'])
        # A late ordinary update must not reopen a completed projection or
        # remove its retention deadline.
        await self.repository.remember('image-1', 'Готово!')
        self.assertIs(self.documents['image-1']['finished'], True)
        self.assertEqual(self.documents['image-1']['expires_at'], terminal['expires_at'])
        self.assertFalse(await self.repository.remember('missing', 'Готово!', finished=True))
        self.assertNotIn('missing', self.documents)

    def test_invalid_retention_is_rejected_before_any_database_call(self):
        for seconds in (0, -1, True, float('inf'), float('nan'), '3600'):
            with self.subTest(seconds=seconds), self.assertRaises(ValueError):
                GenerationProgressRepository(self.collection, retention_seconds=seconds)
