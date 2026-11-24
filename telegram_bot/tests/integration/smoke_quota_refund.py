"""Opt-in Mongo smoke for quota refunds and admission/history retention indexes.

Run against a disposable localhost Mongo; the script creates and drops its own
random database:  python smoke_quota_refund.py mongodb://127.0.0.1:27199
"""
import asyncio
import hashlib
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

REPOSITORY = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(REPOSITORY / 'telegram_bot'), str(REPOSITORY)]

from pymongo import AsyncMongoClient

from imager_config.loader import config
from imager_contracts import make_submission
from src.infrastructure.data.errors import DailyQuotaExceeded
from src.infrastructure.data.repositories.admission_repository import AdmissionRepository, DailyAdmissionLimitExceeded


def submission(job_id, actor_id):
    def reference(key):
        return {'bucket': 'imager', 'key': key, 'size_bytes': 6,
                'sha256': hashlib.sha256(b'pixels').hexdigest(), 'content_type': 'image/jpeg'}
    return make_submission({
        'job_id': job_id, 'source': reference(f'uploads/{job_id}/source.jpg'),
        'parameters': {'group_name': 'animals', 'insertion_format': 'crop', 'alpha_channel': 30,
                       'noise_level': 15, 'cell_size': 40, 'result_size': 120},
        'catalog_manifest': reference('catalog/animals/manifest.json'), 'seed': 123,
        'client_id': 'telegram', 'client_context': {'user_id': actor_id, 'chat_id': actor_id},
    })


async def exercise(database):
    repository = AdmissionRepository(
        database['admissions'], timezone='Europe/Moscow', daily_limit=5, lease_seconds=60, retry_seconds=3,
    )
    await repository.initialize_indexes()
    now = datetime.now(timezone.utc)
    actor = 42
    for index in range(5):
        await repository.accept_submission(submission(f'job{index}', actor), actor_id=actor, now=now)
    assert await repository.remaining(actor, now=now) == 0
    try:
        await repository.accept_submission(submission('job5', actor), actor_id=actor, now=now)
        raise AssertionError('sixth generation must be refused')
    except DailyQuotaExceeded:
        pass

    # A failed generation returns exactly one unit, however often it is reported.
    assert await repository.refund('job0') is True
    assert await repository.refund('job0') is False
    assert await repository.remaining(actor, now=now) == 1
    await repository.accept_submission(submission('job5', actor), actor_id=actor, now=now)
    assert await repository.remaining(actor, now=now) == 0
    assert await repository.refund('unknown-job') is False

    # An administrator's exempt admission stops reducing the ordinary limit too.
    admin = 7
    await repository.accept_submission(submission('admin1', admin), actor_id=admin, now=now, quota_exempt=True)
    assert await repository.remaining(admin, now=now) == 4
    assert await repository.refund('admin1') is True
    assert await repository.remaining(admin, now=now) == 5
    assert await repository.refund('admin1') is False

    # Refunded failures cannot grow one ordinary BSON bucket indefinitely.
    retrying_actor = 88
    for index in range(config.bot.max_daily_admissions):
        job_id = f'refunded-{index}'
        await repository.accept_submission(submission(job_id, retrying_actor), actor_id=retrying_actor, now=now)
        assert await repository.refund(job_id, now=now)
    assert await repository.remaining(retrying_actor, now) == 5
    for operation in (
        lambda: repository.ensure_admission_capacity(retrying_actor, now=now),
        lambda: repository.accept_submission(submission('excess-attempt', retrying_actor), actor_id=retrying_actor, now=now),
    ):
        try:
            await operation()
            raise AssertionError('daily attempt budget must reject further new receipts')
        except DailyAdmissionLimitExceeded:
            pass
    replay = await repository.accept_submission(submission('refunded-0', retrying_actor), actor_id=retrying_actor, now=now)
    assert replay.duplicate
    bucket_id, _, _ = repository._day(retrying_actor, now)
    bucket = await database['admissions'].find_one({'_id': bucket_id})
    assert len(bucket['admissions']) == config.bot.max_daily_admissions and 'expires_at' not in bucket

    # Lifecycle TTL protects accepted commands and pending notifications.
    lifecycle = AdmissionRepository(
        database['lifecycle_admissions'], timezone='Europe/Moscow', daily_limit=5, lease_seconds=60, retry_seconds=3,
    )
    await lifecycle.initialize_indexes()
    await lifecycle.accept_submission(submission('lifecycle', 99), actor_id=99, now=now)
    claim = await lifecycle.claim(now=now)
    assert await lifecycle.acknowledge(claim.job_id, claim.token, now=now)
    bucket = await database['lifecycle_admissions'].find_one({'admissions.job_id': 'lifecycle'})
    assert 'expires_at' not in bucket  # administrator fanout remains pending
    assert await lifecycle.initialize_admin_notice('lifecycle', [], now=now)
    assert await lifecycle.complete_admin_notice('lifecycle', now=now)
    bucket = await database['lifecycle_admissions'].find_one({'admissions.job_id': 'lifecycle'})
    assert bucket['expires_at'] > now
    await lifecycle.accept_submission(submission('lifecycle-new', 99), actor_id=99, now=now)
    bucket = await database['lifecycle_admissions'].find_one({'admissions.job_id': 'lifecycle'})
    assert 'expires_at' not in bucket

    indexes = await database['admissions'].index_information()
    assert 'admission_bucket_ttl' not in indexes
    ttl = indexes['admission_bucket_expiry_ttl']
    assert ttl['key'] == [('expires_at', 1)]
    assert ttl['expireAfterSeconds'] == 0
    print('quota refund smoke: OK')


async def main(uri):
    client = AsyncMongoClient(uri, tz_aware=True, serverSelectionTimeoutMS=5000)
    name = f'refund_smoke_{uuid4().hex[:12]}'
    try:
        await exercise(client[name])
    finally:
        await client.drop_database(name)
        await client.close()


if __name__ == '__main__':
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else 'mongodb://127.0.0.1:27199'))
