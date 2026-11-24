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
from src.infrastructure.data.repositories.admission_repository import AdmissionRepository


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

    indexes = await database['admissions'].index_information()
    ttl = indexes['admission_bucket_ttl']
    assert ttl['key'] == [('created_at', 1)]
    assert ttl['expireAfterSeconds'] == config.bot.admission_retention_days * 24 * 3600
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
