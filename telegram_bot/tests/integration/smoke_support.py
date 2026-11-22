"""Exercise support Mongo filters in one owned random database, without Telegram."""
import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / 'telegram_bot'), str(ROOT)]

from pymongo import AsyncMongoClient

from settings import settings
from src.support.config import support_config
from src.support.repository import SupportQueueFull, SupportRateLimited, SupportRepository


PREFIX = 'imager_support_audit_'


def assert_owned_name(name):
    if (not re.fullmatch(PREFIX + '[0-9a-f]{32}', name)
            or name in {settings.database_name, settings.imager_database_name}):
        raise ValueError('Support smoke accepts only its own random audit database')


def payload(identifier=1, actor=7):
    return {'chat_id': actor, 'user_id': actor, 'message_id': identifier, 'name': 'Audit',
            'username': 'audit', 'text': 'Help', 'content_type': 'text'}


async def exercise(database):
    assert_owned_name(database.name)
    if set(await database.list_collection_names()) - {'_audit_owner'}:
        raise ValueError('Support smoke refuses a nonempty database')
    collection = database['messages']
    repo = SupportRepository(collection, bot_id=100)
    await repo.ensure_indexes()
    indexes = await collection.index_information()
    assert indexes['expires_at_1']['expireAfterSeconds'] == 0

    assert await repo.accept_user_message(payload(), [99, 99, 101])
    assert not await repo.accept_user_message(payload(), [99])
    assert await repo.pending_count(7) == 1
    assert await repo.stored_count() == 1
    assert await repo.next_delivery('support_copy', active_admin_ids=[]) is None
    await repo.activate_admin(99)
    assert await repo.active_admin_ids() == [99]
    marker = await collection.find_one({'kind': 'activation'})
    assert 'expires_at' not in marker

    candidate = await repo.next_delivery('support_copy', active_admin_ids=[99])
    event_id = candidate['event_id']
    assert set(candidate['event']['deliveries']['99']) == {'support_copy'}
    claims = await asyncio.gather(*[repo.reserve_delivery(event_id, 99, 'support_copy') for _ in range(12)])
    assert sum(claims) == 1
    assert await repo.finish_delivery(event_id, 99, 'support_copy', state='sent', message_id=400)
    assert await repo.next_delivery('support_copy', active_admin_ids=[99]) is None
    try:
        await repo.next_delivery('main_alert')
    except ValueError:
        pass
    else:
        raise AssertionError('Support unexpectedly has a main-bot delivery channel')

    pending = await repo.next_delivery('support_copy', active_admin_ids=[101])
    assert pending['event_id'] == event_id and pending['recipient_id'] == 101
    assert await repo.reserve_delivery(event_id, 101, 'support_copy')
    retry = datetime.now(timezone.utc) + timedelta(seconds=60)
    assert await repo.finish_delivery(event_id, 101, 'support_copy', state='pending', retry_at=retry)
    assert await repo.next_delivery('support_copy', active_admin_ids=[101]) is None
    assert (await repo.next_delivery('support_copy', active_admin_ids=[101], now=retry))['event_id'] == event_id
    # A message from another customer is not hidden behind the future retry.
    await repo.activate_admin(101)
    assert await repo.accept_user_message(payload(1, actor=8), [99])
    assert (await repo.next_delivery('support_copy', active_admin_ids=await repo.active_admin_ids()))['recipient_id'] == 99

    await repo.bind_admin_message(99, 40, 7, 1, event_id)
    target = await repo.get_reply_target(99, 40)
    assert target['user_chat_id'] == 7
    assert await repo.get_reply_target(101, 40) is None
    other = SupportRepository(collection, bot_id=101)
    assert await other.get_reply_target(99, 40) is None
    assert await repo.accept_admin_reply(payload(41, actor=99), target)
    assert not await repo.accept_admin_reply(payload(41, actor=99), target)
    answer = await repo.next_delivery('outbound_reply')
    assert answer['recipient_id'] == 7 and answer['event']['admin_id'] == 99
    assert await repo.reserve_delivery(answer['event_id'], 7, 'outbound_reply')
    # A process replacement sees the reservation as uncertain, never pending.
    replacement = SupportRepository(collection, bot_id=100)
    assert await replacement.next_delivery('outbound_reply') is None

    bounded = SupportRepository(database['capacity'], bot_id=100,
                                 policy=replace(support_config, max_pending_per_user=1,
                                                max_pending_total=2, max_stored_events=4))
    results = await asyncio.gather(*[bounded.accept_user_message(payload(identifier), [99])
                                    for identifier in (1, 2)], return_exceptions=True)
    assert sum(result is True for result in results) == 1
    assert sum(isinstance(result, SupportQueueFull) for result in results) == 1
    assert await bounded.accept_user_message(payload(1, actor=8), [99])
    try:
        await bounded.accept_user_message(payload(1, actor=9), [99])
    except SupportQueueFull:
        pass
    else:
        raise AssertionError('Global support capacity was exceeded')

    old = datetime.now(timezone.utc) - timedelta(seconds=support_config.retention_seconds + 1)
    expired = SupportRepository(database['expired'], bot_id=100)
    await expired.accept_user_message(payload(), [99], now=old)
    assert await expired.pending_count() == 0 and await expired.stored_count() == 0
    assert await expired.next_delivery('support_copy') is None
    assert not await expired.reserve_delivery('in:100:7:1', 99, 'support_copy')

    # These identities are necessarily BSON Int64 at the real Mongo boundary.
    large = SupportRepository(database['large_ids'], bot_id=9000000001)
    await large.accept_user_message(payload(actor=6000000000), [6000000001])
    await large.activate_admin(6000000001)
    candidate = await large.next_delivery('support_copy', active_admin_ids=await large.active_admin_ids())
    event = candidate['event']
    assert await large.reserve_delivery(candidate['event_id'], candidate['recipient_id'], 'support_copy')
    assert await large.finish_delivery(candidate['event_id'], candidate['recipient_id'], 'support_copy',
                                       state='sent', message_id=50)
    assert await database['large_ids'].count_documents({'kind': 'mapping'}) == 0
    recovered = await large.get_reply_target(6000000001, 50)
    assert recovered['user_chat_id'] == 6000000000 and type(recovered['user_chat_id']) is int
    assert await large.get_reply_target(6000000002, 50) is None
    await large.bind_admin_message(candidate['recipient_id'], 50, event['user_chat_id'],
                                   event['source_message_id'], candidate['event_id'])
    target = await large.get_reply_target(6000000001, 50)
    assert type(target['user_chat_id']) is int and target['user_chat_id'] == 6000000000
    assert await large.accept_admin_reply(payload(51, actor=6000000001), target)
    answer = await large.next_delivery('outbound_reply')
    assert answer['recipient_id'] == 6000000000
    assert await large.reserve_delivery(answer['event_id'], answer['recipient_id'], 'outbound_reply')

    rate = SupportRepository(database['rate'], bot_id=100)
    await rate.ensure_indexes()
    moment = datetime.now(timezone.utc).replace(microsecond=0)
    burst = await asyncio.gather(*[
        rate.accept_user_message(payload(identifier), [], now=moment) for identifier in range(1, 13)
    ], return_exceptions=True)
    assert sum(item is True for item in burst) == 1
    assert sum(isinstance(item, SupportRateLimited) for item in burst) == 11
    stored = await database['rate'].find_one({'kind': 'incoming'})
    replacement = SupportRepository(database['rate'], bot_id=100)
    assert not await replacement.accept_user_message(payload(stored['source_message_id']), [], now=moment)
    for identifier in range(20, 24):
        await replacement.accept_user_message(payload(identifier), [], now=moment + timedelta(seconds=(identifier - 19) * 10))
    try:
        await replacement.accept_user_message(payload(30), [], now=moment + timedelta(seconds=50))
    except SupportRateLimited as error:
        assert error.retry_after_seconds == 550
    else:
        raise AssertionError('Restart or delivered messages bypassed the rolling support quota')
    assert await replacement.accept_user_message(payload(30), [], now=moment + timedelta(seconds=600))

    daily = SupportRepository(database['daily'], bot_id=100)
    morning = moment.replace(hour=9, minute=0, second=0)
    for identifier in range(1, 21):
        assert await daily.accept_user_message(payload(identifier), [], now=morning + timedelta(seconds=(identifier - 1) * 601))
    try:
        await SupportRepository(database['daily'], bot_id=100).accept_user_message(
            payload(21), [], now=morning + timedelta(seconds=20 * 601))
    except SupportRateLimited as error:
        assert error.retry_at == morning.replace(hour=21)
    else:
        raise AssertionError('Daily support quota was bypassed after process replacement')
    assert await daily.accept_user_message(payload(21), [], now=morning.replace(hour=21))
    return {'passed': True, 'parallel_reservations': len(claims), 'reserved_once': sum(claims),
            'unique_acceptance': True, 'bot_admin_mapping_isolation': True,
            'reply_routing': True, 'unknown_delivery_never_retried': True,
            'explicit_floodwait_scheduling': True, 'capacity_and_retention': True,
            'activation_does_not_expire': True, 'support_only_channel': True,
            'parallel_incoming_admitted_once': True, 'durable_cooldown_window_and_calendar_day': True,
            'large_bot_user_admin_bson_int64_roundtrip': True, 'reply_map_projection_recovered_from_receipt': True}


async def main():
    name, owner = PREFIX + uuid4().hex, uuid4().hex
    assert_owned_name(name)
    client = AsyncMongoClient(settings.mongodb_uri, tz_aware=True, serverSelectionTimeoutMS=5000)
    owned = False
    try:
        if name in await client.list_database_names():
            raise RuntimeError('Random audit database already exists; refusing to reuse it')
        await client[name]['_audit_owner'].insert_one({'_id': owner})
        owned = True
        result = await exercise(client[name])
    finally:
        try:
            if owned:
                assert_owned_name(name)
                if await client[name]['_audit_owner'].find_one({'_id': owner}) is None:
                    raise RuntimeError('Audit database ownership marker is missing')
                await client.drop_database(name)
                if name in await client.list_database_names():
                    raise RuntimeError('Support audit database cleanup did not complete')
        finally:
            await client.close()
    result['audit_database_removed'] = True
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    asyncio.run(main())
