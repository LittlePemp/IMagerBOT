"""Opt-in Mongo smoke; only creates/deletes its own random audit database.

Run against a disposable project-specific localhost Mongo container. The script
does not contact Telegram, MinIO or an existing application database.
"""
import argparse
import asyncio
import hashlib
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

REPOSITORY = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(REPOSITORY / 'telegram_bot'), str(REPOSITORY)]

from pymongo import AsyncMongoClient

from imager_contracts import make_submission
from src.infrastructure.data.errors import AdmissionConflict, DailyQuotaExceeded
from src.infrastructure.data.repositories.admission_repository import AdmissionRepository


def submission(job_id, actor_id=1, noise=15):
    def reference(key):
        return {'bucket': 'imager', 'key': key, 'size_bytes': 6,
                'sha256': hashlib.sha256(b'pixels').hexdigest(), 'content_type': 'image/jpeg'}
    return make_submission({
        'job_id': job_id, 'source': reference(f'uploads/{job_id}/source.jpg'),
        'parameters': {'group_name': 'animals', 'insertion_format': 'crop', 'alpha_channel': 30,
                       'noise_level': noise, 'cell_size': 40, 'result_size': 120},
        'catalog_manifest': reference('catalog/animals/manifest.json'), 'seed': 123,
        'client_id': 'telegram', 'client_context': {'user_id': actor_id, 'chat_id': actor_id},
    })


def repository(collection):
    return AdmissionRepository(
        collection, timezone='Europe/Moscow', daily_limit=5, lease_seconds=60, retry_seconds=3,
    )


async def exercise(client, database):
    repo = repository(database['admissions'])
    await repo.initialize_indexes()
    now = datetime(2026, 11, 12, 12, tzinfo=timezone.utc)
    documents = [submission(f'concurrent-{index}') for index in range(6)]
    results = await asyncio.gather(*[
        repo.accept_submission(document, actor_id=1, now=now) for document in documents
    ], return_exceptions=True)
    assert sum(isinstance(result, DailyQuotaExceeded) for result in results) == 1, results
    assert sum(not isinstance(result, BaseException) for result in results) == 5, results
    assert await repo.remaining(1, now) == 0
    assert await repo.pending_count() == 5
    admitted = next(document for document, result in zip(documents, results)
                    if not isinstance(result, BaseException))
    replay = await repo.accept_submission(admitted, actor_id=1, now=now + timedelta(days=1))
    assert replay.duplicate and replay.date == '2026-11-12'
    assert await repo.remaining(1, now + timedelta(days=1)) == 5
    try:
        await repo.accept_submission(submission(admitted['_id'], noise=20), actor_id=1, now=now)
    except AdmissionConflict:
        pass
    else:
        raise AssertionError('Changed payload was accepted')
    await repo.accept_submission(submission('next-day'), actor_id=1, now=now + timedelta(days=1))
    assert await repo.remaining(1, now + timedelta(days=1)) == 4
    assert await repo.pending_count() == 6
    assert repo.day_window(datetime(2026, 11, 12, 21, 1, tzinfo=timezone.utc)) == (
        datetime(2026, 11, 12, 21, tzinfo=timezone.utc),
        datetime(2026, 11, 13, 21, tzinfo=timezone.utc),
    )

    # Force the cross-bucket uniqueness race at Mongo's index, independently of
    # the initial canonical-receipt lookup, which normally returns the replay.
    tomorrow = await database['admissions'].find_one({'_id': '1:2026-11-13'})
    original_count = tomorrow['used']
    from pymongo.errors import DuplicateKeyError
    try:
        await database['admissions'].update_one(
            {'_id': tomorrow['_id']},
            {'$inc': {'used': 1}, '$push': {'admissions': {
                'job_id': admitted['_id'], 'state': 'pending',
            }}},
        )
    except DuplicateKeyError:
        pass
    else:
        raise AssertionError('Unique multikey index accepted a cross-day duplicate')
    assert (await database['admissions'].find_one({'_id': tomorrow['_id']}))['used'] == original_count

    lease_repo = repository(database['lease_probe'])
    await lease_repo.initialize_indexes()
    frozen = submission('leased-job', actor_id=2)
    await lease_repo.accept_submission(frozen, actor_id=2, now=now)
    frozen['execution_policy']['render']['maximum_attempts'] += 1
    first = await lease_repo.claim(now)
    assert first.document != frozen  # The accepted policy is a deep frozen snapshot.
    assert await lease_repo.claim(now + timedelta(seconds=59)) is None
    second = await lease_repo.claim(now + timedelta(seconds=61))
    assert second.job_id == first.job_id and second.token != first.token
    assert not await lease_repo.acknowledge(first.job_id, first.token, now=now)
    assert await lease_repo.pending_count() == 1
    assert await lease_repo.acknowledge(second.job_id, second.token, now=now)
    assert await lease_repo.pending_count() == 0
    assert (await lease_repo.find('leased-job')).state == 'sent'

    migration = repository(database['migration_probe'])
    await migration.initialize_indexes()
    await migration.ensure_day(3, initial_used=4, now=now)
    await migration.accept_submission(submission('fifth', actor_id=3), actor_id=3, now=now)
    await migration.ensure_day(3, initial_used=0, now=now)
    assert await migration.remaining(3, now) == 0
    indexes = await database['admissions'].index_information()
    assert indexes['admissions.job_id_1']['unique']
    return {'server': (await client.server_info())['version'], 'parallel_requests': 6,
            'accepted': 5, 'rejected_by_daily_quota': 1, 'duplicate_replay': 'passed',
            'cross_day_unique_index_rollback': 'passed', 'moscow_calendar_reset': 'passed',
            'frozen_execution_policy': 'passed', 'lease_fencing': 'passed',
            'pending_count': 'passed', 'legacy_day_seed': 'passed'}


async def main(uri):
    parsed = urlsplit(uri)
    if parsed.scheme != 'mongodb' or parsed.hostname not in ('127.0.0.1', 'localhost', '::1'):
        raise ValueError('The smoke accepts only a disposable localhost Mongo endpoint')
    client = AsyncMongoClient(uri, tz_aware=True, serverSelectionTimeoutMS=3000,
                              connectTimeoutMS=1000, socketTimeoutMS=5000)
    name = f'imager_bot_admission_audit_{uuid4().hex}'
    try:
        await client.admin.command('ping')
        result = await exercise(client, client[name])
        print(json.dumps(result, sort_keys=True))
    finally:
        try:
            await client.drop_database(name)
        finally:
            await client.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--uri', required=True)
    asyncio.run(main(parser.parse_args().uri))
