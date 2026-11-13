"""Opt-in real Mongo checks in one fresh, owned audit database; no Telegram I/O.

Run ``python tests/integration/smoke_admin_notices.py`` in a configured bot
container. The URI stays in settings; only the random audit database is written
or removed. ``exercise(database)`` is also reusable by deployment verification.
"""
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import sys
from uuid import uuid4

REPOSITORY = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(REPOSITORY / 'telegram_bot'), str(REPOSITORY)]

from pymongo import AsyncMongoClient

from imager_config.loader import config
from imager_contracts import make_submission
from settings import settings
from src.infrastructure.data.repositories.admission_repository import AdmissionRepository


DATABASE_PREFIX = 'imager_admin_notice_audit_'


def assert_audit_name(name):
    if (not re.fullmatch(DATABASE_PREFIX + r'[0-9a-f]{32}', name)
            or name in {settings.database_name, settings.imager_database_name,
                        config.mongo.bot_database, config.mongo.core_database}):
        raise ValueError('Administrator notice smoke requires its own random audit database')


def repository(collection):
    return AdmissionRepository(
        collection, timezone=config.bot.timezone, daily_limit=config.bot.regular_user_upload_limit,
        lease_seconds=config.bot.admission_lease_seconds, retry_seconds=config.bot.admission_retry_seconds,
    )


def submission(job_id, actor_id):
    def reference(key, content_type='image/jpeg'):
        return {'bucket': 'imager-audit', 'key': key, 'size_bytes': 6,
                'sha256': hashlib.sha256(b'pixels').hexdigest(), 'content_type': content_type}

    return make_submission({
        'job_id': job_id, 'source': reference(f'uploads/{job_id}/source.jpg'),
        'parameters': {'group_name': 'audit', 'insertion_format': 'crop', 'alpha_channel': 30,
                       'noise_level': 30, 'cell_size': 40, 'result_size': 64},
        'catalog_manifest': reference('catalog/audit/manifest.json', 'application/json'),
        'seed': 123, 'client_id': 'telegram', 'client_context': {'user_id': actor_id, 'chat_id': actor_id},
    })


async def entry(repo, job_id):
    found = await repo._find_entry(job_id)
    assert found is not None
    return found[1]


async def publish(repo, job_id, now):
    """Use the actual bot publication lease, without touching a core inbox."""
    claim = await repo.claim(now=now)
    assert claim is not None and claim.job_id == job_id
    assert await repo.acknowledge(job_id, claim.token, now=now)


async def prepared(repo, job_id, now, recipients):
    await repo.accept_submission(submission(job_id, 1), actor_id=1, now=now)
    await publish(repo, job_id, now)
    assert await repo.initialize_admin_notice(job_id, recipients, now=now)


async def exercise(database):
    """Exercise real filters/indexes/CAS using only this guarded audit database."""
    assert_audit_name(database.name)
    if await database.list_collection_names():
        # main creates only its ownership marker before entering the exercise.
        names = set(await database.list_collection_names())
        if names != {'_audit_owner'}:
            raise ValueError('Administrator notice smoke refuses nonempty audit databases')
    now = datetime.now(timezone.utc).replace(microsecond=0)

    async def fresh(name):
        repo = repository(database[name])
        await repo.initialize_indexes()
        return repo

    repo = await fresh('fanout')
    indexes = await database['fanout'].index_information()
    notice_key = [('admissions.state', 1), ('admissions.admin_notice.state', 1),
                  ('admissions.admin_notice.next_attempt_at', 1)]
    assert any(info['key'] == notice_key for info in indexes.values())
    assert any(info['key'] == [('admissions.job_id', 1)] and info.get('unique')
               for info in indexes.values())
    assert all('expireAfterSeconds' not in info for info in indexes.values())

    document = submission('fanout-original', 1)
    frozen = deepcopy(document)
    accepted = await repo.accept_submission(document, actor_id=1, now=now)
    assert not accepted.duplicate
    initial = await entry(repo, document['_id'])
    assert initial['admin_notice'] == {'state': 'pending', 'recipients': None, 'next_attempt_at': now}
    assert initial['document']['request'] == frozen['request']
    assert initial['document']['payload_hash'] == frozen['payload_hash']
    assert 'admin_notice' not in initial['document']
    assert await repo.next_admin_notice(now=now) is None
    await publish(repo, document['_id'], now)
    selected = await repo.next_admin_notice(now=now)
    assert selected['job_id'] == document['_id'] and selected['actor_id'] == 1
    assert selected['document']['request'] == frozen['request']
    assert await repo.initialize_admin_notice(document['_id'], [101, 101, 102], now=now)
    assert not await repo.initialize_admin_notice(document['_id'], [103], now=now)
    claims = await asyncio.gather(*[
        repo.claim_admin_notice_recipient(document['_id'], 101, now=now) for _ in range(12)
    ])
    assert sum(claims) == 1
    assert await repo.update_admin_notice_recipient(document['_id'], 101, state='sent', message_id=1, now=now)
    assert await repo.claim_admin_notice_recipient(document['_id'], 102, now=now)
    retry_at = now + timedelta(minutes=1)
    assert await repo.update_admin_notice_recipient(document['_id'], 102, state='pending',
                                                   retry_at=retry_at, error='Explicit FloodWait', now=now)
    assert not await repo.complete_admin_notice(document['_id'], now=now)
    assert await repo.next_admin_notice(now=now) is None
    before_replay = await entry(repo, document['_id'])
    assert (await repo.accept_submission(document, actor_id=1, now=now + timedelta(days=1))).duplicate
    assert await entry(repo, document['_id']) == before_replay
    assert not await repo.claim_admin_notice_recipient(document['_id'], 101, now=retry_at)
    assert not await repo.claim_admin_notice_recipient(document['_id'], 102, now=now)

    await repo.accept_submission(submission('fanout-later', 2), actor_id=2, now=now + timedelta(seconds=1))
    await publish(repo, 'fanout-later', now + timedelta(seconds=1))
    assert (await repo.next_admin_notice(now + timedelta(seconds=2)))['job_id'] == 'fanout-later'
    assert await repo.initialize_admin_notice('fanout-later', [], now=now)
    assert await repo.complete_admin_notice('fanout-later', now=now)
    assert (await repo.next_admin_notice(retry_at))['job_id'] == document['_id']
    assert await repo.claim_admin_notice_recipient(document['_id'], 102, now=retry_at)
    assert await repo.update_admin_notice_recipient(document['_id'], 102, state='failed',
                                                   error='Definite rejection', now=retry_at)
    assert await repo.complete_admin_notice(document['_id'], now=retry_at)
    assert await repo.next_admin_notice(retry_at + timedelta(days=1)) is None

    legacy = await fresh('legacy')
    await legacy.accept_submission(submission('legacy-entry', 1), actor_id=1, now=now)
    changed = await database['legacy'].update_one(
        {'admissions.job_id': 'legacy-entry'}, {'$unset': {'admissions.$.admin_notice': ''}},
    )
    assert changed.modified_count == 1
    await publish(legacy, 'legacy-entry', now)
    await legacy.accept_submission(submission('unpublished-entry', 1), actor_id=1, now=now)
    assert await legacy.next_admin_notice(now=now) is None

    fifo = await fresh('fifo')
    for identifier, actor_id, offset in [('first', 1, 0), ('other', 2, 1), ('latest', 1, 2)]:
        await fifo.accept_submission(submission(identifier, actor_id), actor_id=actor_id,
                                     now=now + timedelta(seconds=offset))
    for identifier in ['first', 'other', 'latest']:
        await publish(fifo, identifier, now + timedelta(seconds=3))
    assert (await fifo.next_admin_notice(now + timedelta(seconds=3)))['job_id'] == 'first'
    await fifo.initialize_admin_notice('first', [], now=now)
    await fifo.complete_admin_notice('first', now=now)
    assert (await fifo.next_admin_notice(now + timedelta(seconds=3)))['job_id'] == 'other'

    uncertain = await fresh('uncertain')
    await prepared(uncertain, 'unknown-send', now, [101])
    assert await uncertain.claim_admin_notice_recipient('unknown-send', 101, now=now)
    restarted = repository(database['uncertain'])
    assert await restarted.complete_admin_notice('unknown-send', now=now)
    assert not await restarted.claim_admin_notice_recipient('unknown-send', 101, now=now + timedelta(days=1))
    assert await restarted.next_admin_notice(now + timedelta(days=1)) is None
    assert (await entry(restarted, 'unknown-send'))['admin_notice']['recipients']['101']['state'] == 'uncertain'

    race_before = await fresh('race_retry_before_completion')
    await prepared(race_before, 'race-before', now, [101])
    assert await race_before.claim_admin_notice_recipient('race-before', 101, now=now)
    original_find = race_before._find_entry

    async def snapshot_then_retry(job_id):
        snapshot = await original_find(job_id)
        assert await race_before.update_admin_notice_recipient(job_id, 101, state='pending',
                                                               retry_at=retry_at, now=now)
        return snapshot

    race_before._find_entry = snapshot_then_retry
    try:
        assert not await race_before.complete_admin_notice('race-before', now=now)
    finally:
        race_before._find_entry = original_find
    notice = (await entry(race_before, 'race-before'))['admin_notice']
    assert notice['state'] == 'pending' and notice['recipients']['101']['state'] == 'pending'
    assert not await race_before.complete_admin_notice('race-before', now=now)
    assert await race_before.next_admin_notice(now) is None
    assert (await race_before.next_admin_notice(retry_at))['job_id'] == 'race-before'

    race_after = await fresh('race_retry_after_completion')
    await prepared(race_after, 'race-after', now, [101])
    assert await race_after.claim_admin_notice_recipient('race-after', 101, now=now)
    assert await race_after.complete_admin_notice('race-after', now=now)
    assert await race_after.update_admin_notice_recipient('race-after', 101, state='pending',
                                                          retry_at=retry_at, now=now)
    assert not await race_after.complete_admin_notice('race-after', now=now)
    assert await race_after.next_admin_notice(now) is None
    assert (await race_after.next_admin_notice(retry_at))['job_id'] == 'race-after'

    empty = await fresh('empty_or_revoked')
    await prepared(empty, 'empty', now, [])
    assert await empty.complete_admin_notice('empty', now=now)
    await prepared(empty, 'revoked', now + timedelta(seconds=1), [101])
    assert await empty.skip_admin_notice_recipient('revoked', 101, now=now)
    assert not await empty.claim_admin_notice_recipient('revoked', 101, now=now + timedelta(days=1))
    assert await empty.complete_admin_notice('revoked', now=now)

    names = await database.list_collection_names()
    records = sum([await database[name].count_documents({}) for name in names])
    assert records < 100
    return {
        'passed': True, 'parallel_claims': len(claims), 'reserved_attempts': sum(claims),
        'atomic_notice_and_duplicate_replay': True, 'immutable_core_payload': True,
        'new_published_only': True, 'fifo_across_buckets': True,
        'uncertain_never_retried': True, 'sent_recipient_never_repeated': True,
        'floodwait_deferral_and_no_starvation': True, 'completion_retry_race_both_orders': True,
        'empty_and_revoked_recipients': True, 'indexes_created_without_ttl': True,
        'audit_records': records,
    }


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
                marker = await client[name]['_audit_owner'].find_one({'_id': owner})
                if marker is None:
                    raise RuntimeError('Audit ownership marker is missing; refusing database cleanup')
                await client.drop_database(name)
                if name in await client.list_database_names():
                    raise RuntimeError('Owned audit database cleanup did not complete')
        finally:
            await client.close()
    result['audit_database_removed'] = True
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    asyncio.run(main())
