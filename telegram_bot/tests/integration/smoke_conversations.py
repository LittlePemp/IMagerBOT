"""Real PyMongoStorage migration checks in one owned random audit database.

Run inside a configured bot image: python tests/integration/smoke_conversations.py
No Telegram calls are made. Application databases are never modified.
"""
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import sys
from uuid import uuid4

REPOSITORY = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(REPOSITORY / 'telegram_bot'), str(REPOSITORY)]

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.pymongo import PyMongoStorage
from pymongo import AsyncMongoClient

from imager_config.loader import config
from settings import settings
from src.infrastructure.data.conversation_storage import preserve_conversations


DATABASE_PREFIX = 'imager_conversation_audit_'


def assert_audit_name(name):
    if (not re.fullmatch(DATABASE_PREFIX + r'[0-9a-f]{32}', name)
            or len(name) >= 64
            or name in {settings.database_name, settings.imager_database_name,
                        config.mongo.bot_database, config.mongo.core_database}):
        raise ValueError('Conversation smoke requires its own random audit database')


async def exercise(database):
    assert_audit_name(database.name)
    names = set(await database.list_collection_names())
    if names and names != {'_audit_owner'}:
        raise ValueError('Conversation smoke refuses an existing nonempty audit database')
    storage = PyMongoStorage(database.client, db_name=database.name, collection_name='conversations')
    collection = database['conversations']
    state = FSMContext(storage, StorageKey(bot_id=1, chat_id=7, user_id=7))
    future = datetime.now(timezone.utc) + timedelta(days=1)
    draft = {
        'draft_id': 'audit-draft', 'image_group': 'emoji', 'image_size': 128,
        'noise_level': 30, 'inset_size': 40,
        '_current_view': {
            'token': 'audit-current', 'owner_id': 7, 'chat_id': 7, 'message_id': 9,
            'records': {'0': 'generate_image'},
            'expires_at': future.timestamp(), 'expires_on': future,
        },
    }
    await state.set_state('ImageGenerationStates:waiting_for_image_upload')
    await state.set_data(draft)
    await collection.create_index('data._current_view.expires_on', expireAfterSeconds=0, name='legacy_view_ttl')
    await collection.create_index('other_expiry', expireAfterSeconds=0, name='unrelated_ttl')
    await collection.create_index('data._current_view.owner_id', name='owner_lookup')
    before = await collection.find_one({})
    expected_data = deepcopy(before['data'])
    expected_data['_current_view'].pop('expires_at')
    expected_data['_current_view'].pop('expires_on')
    original_indexes = await collection.index_information()

    result = await preserve_conversations(collection)
    assert result == {'removed_indexes': ['legacy_view_ttl'], 'cleaned_documents': 1}
    after = await collection.find_one({'_id': before['_id']})
    assert after['state'] == before['state']
    assert after['data'] == expected_data
    assert set(after) == set(before)
    indexes = await collection.index_information()
    assert set(indexes) == set(original_indexes) - {'legacy_view_ttl'}
    assert indexes['unrelated_ttl'] == original_indexes['unrelated_ttl']
    assert indexes['owner_lookup'] == original_indexes['owner_lookup']
    assert await state.get_state() == 'ImageGenerationStates:waiting_for_image_upload'
    assert await state.get_data() == expected_data
    assert await preserve_conversations(collection) == {'removed_indexes': [], 'cleaned_documents': 0}
    assert await collection.find_one({'_id': before['_id']}) == after

    # Actual aiogram clear removes the now-empty document without needing TTL.
    await state.clear()
    assert await state.get_state() is None and await state.get_data() == {}
    assert await collection.find_one({'_id': before['_id']}) is None
    # A new form also works after migration; it does not need expiration fields.
    await state.set_state('ImageGenerationStates:waiting_manual_image_size')
    await state.set_data({'draft_id': 'next-draft', 'image_group': 'emoji'})
    assert await state.get_data() == {'draft_id': 'next-draft', 'image_group': 'emoji'}
    await state.clear()
    assert await collection.count_documents({}) == 0
    return {'passed': True, 'legacy_ttl_removed': True, 'unrelated_indexes_preserved': True,
            'form_draft_nonce_and_actions_preserved': True, 'migration_idempotent': True,
            'actual_pymongo_storage_roundtrip': True, 'empty_conversation_removed': True}


async def main():
    name = DATABASE_PREFIX + uuid4().hex
    assert_audit_name(name)
    owner = uuid4().hex
    client = AsyncMongoClient(
        settings.mongodb_uri, tz_aware=True,
        serverSelectionTimeoutMS=config.mongo.server_selection_timeout_ms,
        connectTimeoutMS=config.mongo.connect_timeout_ms,
        socketTimeoutMS=config.mongo.socket_timeout_ms,
        waitQueueTimeoutMS=config.mongo.wait_queue_timeout_ms,
    )
    owned = False
    result = None
    try:
        if name in await client.list_database_names():
            raise RuntimeError('Audit database unexpectedly exists; refusing to reuse it')
        await client[name]['_audit_owner'].insert_one({'_id': owner})
        owned = True
        result = await exercise(client[name])
    finally:
        try:
            if owned:
                assert_audit_name(name)
                if await client[name]['_audit_owner'].find_one({'_id': owner}) is None:
                    raise RuntimeError('Audit ownership marker missing; refusing database cleanup')
                await client.drop_database(name)
                if name in await client.list_database_names():
                    raise RuntimeError('Owned audit database cleanup did not complete')
        finally:
            await client.close()
    result['audit_database_removed'] = True
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    asyncio.run(main())
