"""Durable command submission and completion inbox for the Telegram adapter.

There is no long-running RPC or shared filesystem between the bot and renderer.
Mongo holds immutable command/outbox envelopes; the core dispatcher starts Temporal.
"""
import asyncio
import hashlib
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

from imager_contracts import make_submission
from src.infrastructure.services.object_storage import BotObjectStorage
from src.utils.loggers import service_logger

from .schemas import GenerationAccepted, GroupInfo, ListGroupsResponse, SyncGroupsResponse


def _secret(value):
    return value.get_secret_value() if hasattr(value, 'get_secret_value') else value


class ImagerService:
    def __init__(self, config, *, database=None, object_store=None):
        self.config = config
        self._database = database
        self._mongo_client = None
        self._object_store = object_store

    @property
    def database(self):
        if self._database is None:
            self._mongo_client = MongoClient(
                self.config.mongodb_uri, tz_aware=True, serverSelectionTimeoutMS=10000,
                connectTimeoutMS=5000, socketTimeoutMS=10000, waitQueueTimeoutMS=10000,
            )
            self._database = self._mongo_client[self.config.imager_database_name]
        return self._database

    @property
    def object_store(self):
        if self._object_store is None:
            self._object_store = BotObjectStorage(
                self.config.minio_endpoint,
                _secret(self.config.minio_access_key),
                _secret(self.config.minio_secret_key),
                self.config.minio_bucket, secure=self.config.minio_secure,
            )
        return self._object_store

    @property
    def jobs(self):
        return self.database['generation_jobs']

    async def get_job(self, job_id):
        return await asyncio.to_thread(self.jobs.find_one, {'_id': job_id})

    async def upload_source(self, path, job_id):
        try:
            return await asyncio.to_thread(self.object_store.upload_source, path, job_id)
        except Exception:
            service_logger.exception(f'Source upload failed: job_id={job_id}')
            raise

    async def submit_generation(self, request, *, job_id, client_context):
        """A successful return means command+pending outbox are persisted."""
        group = await asyncio.to_thread(
            self.database['catalog_groups'].find_one, {'_id': request.group_name}
        )
        if not group or not group.get('manifest') or not group.get('count', 0):
            raise ValueError('Выбранная библиотека отсутствует или пуста. Обновите список групп.')
        parameters = request.model_dump(exclude={'source'})
        envelope = {
            'job_id': job_id,
            'source': request.source,
            'parameters': parameters,
            'catalog_manifest': group['manifest'],
            'seed': int(hashlib.sha256(job_id.encode('utf-8')).hexdigest()[:8], 16),
            'client_id': 'telegram',
            'client_context': dict(client_context),
        }
        document = make_submission(envelope)
        try:
            try:
                await asyncio.to_thread(
                    self.jobs.update_one, {'_id': job_id},
                    {'$setOnInsert': document}, upsert=True,
                )
            except DuplicateKeyError:
                # Concurrent delivery of the same Telegram update: verify the
                # already stored envelope rather than replacing its snapshot.
                pass
            stored = await self.get_job(job_id)
            if stored is None or stored.get('payload_hash') != document['payload_hash']:
                raise ValueError('Идентификатор задания уже использован с другими параметрами.')
            service_logger.info(f'Generation command accepted: job_id={job_id}')
            return GenerationAccepted(
                job_id=job_id, status=stored['status'], workflow_id=f'imager:{job_id}',
            )
        except Exception:
            service_logger.exception(f'Generation submission failed: job_id={job_id}')
            raise

    async def list_groups(self):
        documents = await asyncio.to_thread(
            lambda: list(self.database['catalog_groups'].find({}, {'group': 1, 'count': 1}))
        )
        return ListGroupsResponse(groups=[
            GroupInfo(group=item.get('group', item['_id']), count=item['count'])
            for item in documents if item.get('count', 0) > 0
        ])

    async def sync_groups(self):
        groups = await self.list_groups()
        return SyncGroupsResponse(
            message=f'Каталог MinIO обновлён: доступно групп — {len(groups.groups)}.'
        )

    async def claim_completion(self):
        now = datetime.now(timezone.utc)
        token = uuid4().hex
        maximum = self.config.bot_notification_max_attempts
        # A final delivery attempt may have died before its exception handler.
        await asyncio.to_thread(
            self.jobs.update_many,
            {
                'request.client_id': 'telegram',
                'completion_outbox.attempts': {'$gte': maximum},
                '$or': [
                    {'completion_outbox.state': 'pending'},
                    {'completion_outbox.state': 'claimed',
                     'completion_outbox.lease_until': {'$lte': now}},
                ],
            },
            {'$set': {
                'completion_outbox.state': 'failed',
                'completion_outbox.error': 'Telegram delivery attempts exhausted',
            }},
        )
        query = {
            'request.client_id': 'telegram',
            'status': {'$in': ['completed', 'failed']},
            'completion_outbox.attempts': {'$lt': maximum},
            '$or': [
                {'completion_outbox.state': 'pending',
                 'completion_outbox.available_at': {'$lte': now}},
                {'completion_outbox.state': 'pending',
                 'completion_outbox.available_at': {'$exists': False}},
                {'completion_outbox.state': 'claimed',
                 'completion_outbox.lease_until': {'$lte': now}},
            ],
        }
        return await asyncio.to_thread(
            self.jobs.find_one_and_update, query,
            {
                '$set': {
                    'completion_outbox.state': 'claimed',
                    'completion_outbox.token': token,
                    'completion_outbox.lease_until': now + timedelta(
                        seconds=self.config.bot_notification_lease_seconds
                    ),
                },
                '$inc': {'completion_outbox.attempts': 1},
            },
            sort=[('updated_at', 1)], return_document=ReturnDocument.AFTER,
        )

    async def renew_completion(self, job_id, token):
        result = await asyncio.to_thread(
            self.jobs.update_one,
            {'_id': job_id, 'completion_outbox.state': 'claimed', 'completion_outbox.token': token},
            {'$set': {'completion_outbox.lease_until': datetime.now(timezone.utc) + timedelta(
                seconds=self.config.bot_notification_lease_seconds
            )}},
        )
        return result.matched_count == 1

    async def acknowledge_completion(self, job_id, token, message_id):
        result = await asyncio.to_thread(
            self.jobs.update_one,
            {'_id': job_id, 'completion_outbox.state': 'claimed', 'completion_outbox.token': token},
            {'$set': {
                'completion_outbox.state': 'sent',
                'completion_outbox.sent_at': datetime.now(timezone.utc),
                'completion_outbox.message_id': message_id,
                'completion_outbox.lease_until': None,
            }},
        )
        return result.matched_count == 1

    async def fail_completion(self, job_id, token, attempts, error):
        exhausted = attempts >= self.config.bot_notification_max_attempts
        await asyncio.to_thread(
            self.jobs.update_one,
            {'_id': job_id, 'completion_outbox.state': 'claimed', 'completion_outbox.token': token},
            {'$set': {
                'completion_outbox.state': 'failed' if exhausted else 'pending',
                'completion_outbox.error': str(error)[:2000],
                'completion_outbox.available_at': datetime.now(timezone.utc) + timedelta(
                    seconds=min(2 ** attempts, 60)
                ),
                'completion_outbox.lease_until': None,
            }},
        )
        service_logger.error(
            f'Telegram delivery failed: job_id={job_id}, attempts={attempts}, exhausted={exhausted}, error={error}'
        )

    def close(self):
        if self._mongo_client is not None:
            self._mongo_client.close()
