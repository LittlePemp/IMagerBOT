"""The view-TTL migration must retain forms and unrelated indexes."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from pymongo.errors import OperationFailure

from src.infrastructure.data.conversation_storage import preserve_conversations


class ConversationStorageTests(unittest.IsolatedAsyncioTestCase):
    def collection(self, indexes):
        return SimpleNamespace(
            index_information=AsyncMock(return_value=indexes),
            drop_index=AsyncMock(),
            update_many=AsyncMock(return_value=SimpleNamespace(modified_count=2)),
        )

    async def test_only_known_keyboard_ttl_is_dropped_before_touching_metadata(self):
        collection = self.collection({
            '_id_': {'key': [('_id', 1)]},
            'old_view_ttl': {'key': [('data._current_view.expires_on', 1)], 'expireAfterSeconds': 0},
            'unrelated_ttl': {'key': [('expires_at', 1)], 'expireAfterSeconds': 0},
            'other_view_policy': {'key': [('data._current_view.expires_on', -1)], 'expireAfterSeconds': 60},
            'normal_view_index': {'key': [('data._current_view.expires_at', 1)]},
        })
        events = []
        collection.drop_index.side_effect = lambda name: events.append(('drop', name))
        collection.update_many.side_effect = lambda *args: (
            events.append(('metadata', args)) or SimpleNamespace(modified_count=2)
        )
        result = await preserve_conversations(collection)
        self.assertEqual(result, {'removed_indexes': ['old_view_ttl'], 'cleaned_documents': 2})
        collection.drop_index.assert_awaited_once_with('old_view_ttl')
        self.assertEqual(events[0], ('drop', 'old_view_ttl'))
        query, update = events[1][1]
        self.assertEqual(query, {'$or': [
            {'data._current_view.expires_at': {'$exists': True}},
            {'data._current_view.expires_on': {'$exists': True}},
        ]})
        self.assertEqual(update, {'$unset': {
            'data._current_view.expires_at': '', 'data._current_view.expires_on': '',
        }})

    async def test_repeated_startup_and_fresh_database_do_not_require_a_ttl_index(self):
        collection = self.collection({})
        collection.update_many.return_value = SimpleNamespace(modified_count=0)
        for _ in range(2):
            self.assertEqual(await preserve_conversations(collection), {
                'removed_indexes': [], 'cleaned_documents': 0,
            })
        collection.drop_index.assert_not_awaited()

    async def test_concurrent_index_removal_is_safe_but_other_mongo_errors_propagate(self):
        indexes = {'legacy': {'key': [('data._current_view.expires_on', 1)], 'expireAfterSeconds': 0}}
        collection = self.collection(indexes)
        collection.drop_index.side_effect = OperationFailure('already removed', code=27)
        self.assertEqual((await preserve_conversations(collection))['removed_indexes'], ['legacy'])
        collection.update_many.reset_mock()
        collection.drop_index.side_effect = OperationFailure('not permitted', code=13)
        with self.assertRaises(OperationFailure):
            await preserve_conversations(collection)
        collection.update_many.assert_not_awaited()

    async def test_similar_fields_and_custom_ttl_durations_are_preserved(self):
        collection = self.collection({
            'custom': {'key': [('data._current_view.expires_on', 1)], 'expireAfterSeconds': 60},
            'compound': {'key': [('data._current_view.expires_on', 1), ('state', 1)], 'expireAfterSeconds': 0},
            'other_ttl': {'key': [('data.other.expires_on', 1)], 'expireAfterSeconds': 0},
        })
        await preserve_conversations(collection)
        collection.drop_index.assert_not_awaited()
