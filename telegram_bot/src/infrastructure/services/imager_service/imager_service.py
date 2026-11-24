"""Durable command submission and completion inbox for the Telegram adapter.

There is no long-running RPC or shared filesystem between the bot and renderer.
Mongo holds immutable command/outbox envelopes; the core dispatcher starts Temporal.
"""
import asyncio
import hashlib
import re
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

from imager_contracts import JobRequest, ObjectRef, make_submission, payload_fingerprint
from imager_contracts.protocol import GENERATION_SEED_HEX_LENGTH, JPEG_CONTENT_TYPE, JOB_ID_PATTERN, SCHEMA_VERSION
from imager_config.loader import config as runtime_config
from src.infrastructure.services.object_storage import BotObjectStorage
from src.utils.loggers import service_logger

from .schemas import GenerationAccepted, GroupInfo, ListGroupsResponse, SyncGroupsResponse
from .delivery_policy import DeliveryFailureKind, bounded_backoff, delivery_failure


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
                self.config.mongodb_uri, tz_aware=True,
                serverSelectionTimeoutMS=runtime_config.mongo.server_selection_timeout_ms,
                connectTimeoutMS=runtime_config.mongo.connect_timeout_ms,
                socketTimeoutMS=runtime_config.mongo.socket_timeout_ms,
                waitQueueTimeoutMS=runtime_config.mongo.wait_queue_timeout_ms,
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
        return self.database[runtime_config.mongo.jobs_collection]

    async def ensure_indexes(self):
        await asyncio.to_thread(
            self.jobs.create_index,
            [('request.client_id', 1), ('request.client_context.user_id', 1), ('created_at', 1)],
            name='telegram_user_jobs',
        )
        await asyncio.to_thread(
            self.jobs.create_index,
            [('request.client_id', 1), ('status', 1),
             ('telegram_history_projection.state', 1),
             ('telegram_history_projection.available_at', 1),
             ('telegram_history_projection.lease_until', 1)],
            name='telegram_history_repair',
        )

    async def get_job(self, job_id):
        return await asyncio.to_thread(self.jobs.find_one, {'_id': job_id})

    async def latest_user_job(self, actor_id):
        return await asyncio.to_thread(
            self.jobs.find_one,
            {'request.client_id': 'telegram', 'request.client_context.user_id': actor_id},
            sort=[('created_at', -1)],
        )

    async def recent_uploads(self, limit=10) -> list[dict]:
        """Read a bounded Telegram-source gallery from the core job records."""
        if type(limit) is not int:
            raise ValueError('Recent upload limit must be an integer')
        if limit <= 0:
            # Mongo's limit(0) removes the bound instead of returning no rows.
            return []
        limit = min(limit, 10)
        return await asyncio.to_thread(lambda: list(self.jobs.find(
            {
                'request.client_id': 'telegram',
                'request.source': {'$exists': True, '$ne': None},
                'artifact_lifecycle.source.state': {
                    '$nin': ['deleted', 'deleting', 'unmanaged'],
                },
            },
            {
                '_id': 1, 'created_at': 1, 'request.source': 1,
                'request.client_context': 1, 'request.parameters': 1,
                'artifact_lifecycle.source': 1,
            },
        ).sort([('created_at', -1), ('_id', -1)]).limit(limit)))

    async def download_source(self, job_id, path):
        """Recheck ownership/retention before a bounded, verified source download."""
        if not isinstance(job_id, str) or not re.fullmatch(JOB_ID_PATTERN, job_id):
            raise ValueError('Invalid job identifier')
        document = await self.get_job(job_id)
        if not isinstance(document, dict) or document.get('_id') != job_id:
            raise ValueError('Source job was not found')
        request = document.get('request')
        if not isinstance(request, dict) or request.get('client_id') != 'telegram':
            raise ValueError('Source job does not belong to Telegram')
        lifecycle = document.get('artifact_lifecycle') or {}
        if not isinstance(lifecycle, dict):
            raise ValueError('Invalid source artifact lifecycle')
        source_lifecycle = lifecycle.get('source') or {}
        if not isinstance(source_lifecycle, dict):
            raise ValueError('Invalid source artifact lifecycle')
        if source_lifecycle.get('state') in ('deleted', 'deleting', 'unmanaged'):
            raise ValueError('Source artifact is no longer available')
        reference = ObjectRef.from_dict(request.get('source')).to_dict()
        key = reference['key']
        # Keep the same ownership boundary as the core's source retention policy.
        protected = ['catalog/'] + [template.split('{', 1)[0] for template in (
            runtime_config.storage.catalog_image_key_template,
            runtime_config.storage.catalog_manifest_key_template,
            runtime_config.pack_import.archive_key_template,
        )]
        if any(not prefix or key.startswith(prefix) for prefix in protected):
            raise ValueError('Source points into a catalog/import namespace')
        expected = runtime_config.storage.source_key_template.format(
            job_id=job_id, sha256=reference['sha256'],
        )
        legacy = {f'uploads/{job_id}/{filename}' for filename in (
            runtime_config.storage.source_filename,
            runtime_config.storage.upload_filename,
        )}
        if key != expected and key not in legacy:
            raise ValueError('Source artifact is not owned by this job')
        max_bytes = runtime_config.limits.source_file_bytes
        if not 0 < reference['size_bytes'] <= max_bytes:
            raise ValueError('Source file exceeds the configured size limit')
        if reference['bucket'] != self.object_store.bucket:
            raise ValueError('Source uses a different object bucket')
        await asyncio.to_thread(
            self.object_store.download_result, reference, path, max_bytes=max_bytes,
        )
        return str(path)

    async def active_queue(self, *, pending_ids=()):
        query = {'status': {'$in': ['queued', 'running', 'retrying']}}
        if pending_ids:
            query = {'$or': [query, {'_id': {'$in': list(pending_ids)}}]}
        return await asyncio.to_thread(lambda: list(self.jobs.find(
            query,
            {'_id': 1, 'status': 1, 'created_at': 1},
        ).sort([('created_at', 1), ('_id', 1)])))

    async def progress_jobs(self, job_ids):
        return await asyncio.to_thread(lambda: list(self.jobs.find(
            {'_id': {'$in': job_ids}, 'request.client_id': 'telegram'},
            {'status': 1, 'error': 1, 'created_at': 1, 'execution.progress': 1,
             'completion_outbox.state': 1, 'request.client_context': 1},
        )))

    async def count_active_jobs(self):
        return await asyncio.to_thread(
            self.jobs.count_documents, {'status': {'$nin': ['completed', 'failed']}},
        )

    async def count_user_jobs_between(self, actor_id, start, end):
        return await asyncio.to_thread(
            self.jobs.count_documents,
            # Failed generations return their quota unit, so they never count.
            {'request.client_id': 'telegram', 'request.client_context.user_id': actor_id,
             'created_at': {'$gte': start, '$lt': end}, 'status': {'$ne': 'failed'}},
        )

    async def upload_source(self, path, job_id, *, content_type=JPEG_CONTENT_TYPE):
        try:
            return await asyncio.to_thread(self.object_store.upload_source, path, job_id, content_type=content_type)
        except Exception:
            service_logger.exception(f'Source upload failed: job_id={job_id}')
            raise

    async def delete_source(self, job_id, key):
        await asyncio.to_thread(self.object_store.delete_source, job_id, key)

    async def list_sources(self):
        return await asyncio.to_thread(lambda: list(self.object_store.list_sources()))

    async def prepare_submission(self, request, *, job_id, client_context):
        """Resolve the catalog and execution policy once, before durable admission."""
        group = await asyncio.to_thread(
            self.database[runtime_config.mongo.catalog_collection].find_one, {'_id': request.group_name}
        )
        if not group or not group.get('manifest') or not group.get('count', 0):
            raise ValueError('Выбранная библиотека отсутствует или пуста. Обновите список групп.')
        parameters = request.model_dump(exclude={'source', 'watermark_text'})
        envelope = {
            'job_id': job_id,
            'source': request.source,
            'parameters': parameters,
            'catalog_manifest': group['manifest'],
            'seed': int(hashlib.sha256(job_id.encode('utf-8')).hexdigest()[:GENERATION_SEED_HEX_LENGTH], 16),
            'client_id': 'telegram',
            'client_context': dict(client_context),
        }
        if request.watermark_text is not None:
            envelope['watermark_text'] = request.watermark_text
        document = make_submission(envelope)
        document['dispatch']['workflow_id'] = f'{runtime_config.temporal.workflow_id_prefix}{job_id}'
        return document

    async def publish_submission(self, document):
        """Publish the exact admission snapshot; never re-read mutable settings."""
        request = JobRequest.from_dict(document['request'])
        job_id = request.job_id
        if (document.get('_id') != job_id
                or document.get('schema_version') != SCHEMA_VERSION
                or document.get('payload_hash') != payload_fingerprint(request)):
            raise ValueError('Stored admission has an inconsistent request identity')
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
                raise ValueError('Это изображение уже принято с другими параметрами.')
            service_logger.info(f'Generation command accepted: job_id={job_id}')
            return GenerationAccepted(
                job_id=job_id, status=stored['status'],
                workflow_id=(stored.get('dispatch', {}).get('workflow_id')
                             or f'{runtime_config.temporal.workflow_id_prefix}{job_id}'),
            )
        except Exception:
            service_logger.exception(f'Generation submission failed: job_id={job_id}')
            raise

    async def submit_generation(self, request, *, job_id, client_context):
        """Compatibility entry point; success means the core inbox is persisted."""
        document = await self.prepare_submission(
            request, job_id=job_id, client_context=client_context,
        )
        return await self.publish_submission(document)

    async def list_groups(self):
        documents = await asyncio.to_thread(
            lambda: list(self.database[runtime_config.mongo.catalog_collection].find({}, {'group': 1, 'count': 1, 'display_name': 1}))
        )
        return ListGroupsResponse(groups=[
            GroupInfo(group=item.get('group', item['_id']), count=item['count'], display_name=item.get('display_name'))
            for item in documents if item.get('count', 0) > 0
        ])

    async def sync_groups(self):
        groups = await self.list_groups()
        return SyncGroupsResponse(
            message=f'Каталог MinIO обновлён: доступно групп — {len(groups.groups)}.'
        )

    async def claim_completion(self):
        """Reclaim preparation only; a started Telegram send is never repeated."""
        now = datetime.now(timezone.utc)
        token = uuid4().hex
        # A previous process may have sent the document before it stopped.
        # Older pending retries also have an unknown delivery outcome.
        await asyncio.to_thread(
            self.jobs.update_many,
            {
                'request.client_id': 'telegram',
                '$or': [
                    {'completion_outbox.state': 'pending',
                     'completion_outbox.attempts': {'$gt': 0}},
                    {'completion_outbox.state': 'claimed',
                     'completion_outbox.lease_until': {'$lte': now}},
                ],
            },
            {'$set': {
                'completion_outbox.state': 'uncertain',
                'completion_outbox.error': 'Telegram delivery outcome is unknown; resend disabled',
                'completion_outbox.error_kind': 'uncertain',
                'completion_outbox.lease_until': None,
            }},
        )
        query = {
            'request.client_id': 'telegram',
            'status': {'$in': ['completed', 'failed']},
            '$or': [
                {'completion_outbox.state': 'pending',
                 'completion_outbox.attempts': 0,
                 'completion_outbox.available_at': {'$lte': now}},
                {'completion_outbox.state': 'pending',
                 'completion_outbox.attempts': 0,
                 'completion_outbox.available_at': {'$exists': False}},
                {'completion_outbox.state': 'preparing',
                 'completion_outbox.lease_until': {'$lte': now}},
            ],
        }
        return await asyncio.to_thread(
            self.jobs.find_one_and_update, query,
            {
                '$set': {
                    'completion_outbox.state': 'preparing',
                    'completion_outbox.token': token,
                    'completion_outbox.lease_until': now + timedelta(
                        seconds=self.config.bot_notification_lease_seconds
                    ),
                },
            },
            sort=[('updated_at', 1)], return_document=ReturnDocument.AFTER,
        )

    async def mark_delivery_started(self, job_id, token):
        """Persist the non-repeatable boundary immediately before Telegram I/O."""
        now = datetime.now(timezone.utc)
        result = await asyncio.to_thread(
            self.jobs.update_one,
            {'_id': job_id, 'completion_outbox.state': 'preparing',
             'completion_outbox.token': token, 'completion_outbox.lease_until': {'$gt': now}},
            {'$set': {
                'completion_outbox.state': 'claimed',
                'completion_outbox.started_at': now,
                'completion_outbox.attempts': 1,
            }},
        )
        return result.matched_count == 1

    async def renew_completion(self, job_id, token):
        result = await asyncio.to_thread(
            self.jobs.update_one,
            {'_id': job_id, 'completion_outbox.state': {'$in': ['preparing', 'claimed']},
             'completion_outbox.token': token},
            {'$set': {'completion_outbox.lease_until': datetime.now(timezone.utc) + timedelta(
                seconds=self.config.bot_notification_lease_seconds
            )}},
        )
        return result.matched_count == 1

    async def acknowledge_completion(self, job_id, token, message_id):
        result = await asyncio.to_thread(
            self.jobs.update_one,
            # Expiry may have marked the same send uncertain while Telegram was
            # returning its response. Its original token can still record fact.
            {'_id': job_id, 'completion_outbox.state': {'$in': ['claimed', 'uncertain']},
             'completion_outbox.token': token},
            {'$set': {
                'completion_outbox.state': 'sent',
                'completion_outbox.sent_at': datetime.now(timezone.utc),
                'completion_outbox.message_id': message_id,
                'completion_outbox.lease_until': None,
                'completion_outbox.error': None,
                'completion_outbox.error_kind': None,
            }},
        )
        return result.matched_count == 1

    async def fail_completion(self, job_id, token, error, *, send_started=False):
        failure = delivery_failure(error)
        # A transport error after send started can hide an accepted document.
        # Explicit Bot API refusals and preparation errors are definite failures.
        state = ('uncertain' if send_started and failure == DeliveryFailureKind.TRANSIENT
                 else 'failed')
        update = {'$set': {
            'completion_outbox.state': state,
            'completion_outbox.error': str(error)[:runtime_config.bot.error_max_length],
            'completion_outbox.error_kind': failure.value,
            'completion_outbox.available_at': None,
            'completion_outbox.lease_until': None,
        }}
        result = await asyncio.to_thread(
            self.jobs.update_one,
            {'_id': job_id, 'completion_outbox.state': {'$in': ['preparing', 'claimed', 'uncertain']},
             'completion_outbox.token': token},
            update,
        )
        service_logger.error(
            f'Telegram delivery stopped: job_id={job_id}, '
            f'kind={failure.value}, state={state}, error={error}'
        )
        return result.matched_count == 1

    async def claim_history_projection(self):
        """Repair terminal job history independently of Telegram delivery state."""
        now = datetime.now(timezone.utc)
        token = uuid4().hex
        return await asyncio.to_thread(
            self.jobs.find_one_and_update,
            {
                'request.client_id': 'telegram',
                'status': {'$in': ['completed', 'failed']},
                'request.client_context.history_record_id': {'$exists': True},
                '$or': [
                    {'telegram_history_projection.state': {'$exists': False}},
                    {'telegram_history_projection.state': 'pending',
                     'telegram_history_projection.available_at': {'$lte': now}},
                    {'telegram_history_projection.state': 'claimed',
                     'telegram_history_projection.lease_until': {'$lte': now}},
                ],
            },
            {'$set': {
                'telegram_history_projection.state': 'claimed',
                'telegram_history_projection.token': token,
                'telegram_history_projection.lease_until': now + timedelta(
                    seconds=runtime_config.bot.history_repair_lease_seconds,
                ),
            }, '$inc': {'telegram_history_projection.attempts': 1}},
            sort=[('updated_at', 1)], return_document=ReturnDocument.AFTER,
        )

    async def acknowledge_history_projection(self, job_id, token):
        result = await asyncio.to_thread(
            self.jobs.update_one,
            {'_id': job_id, 'telegram_history_projection.state': 'claimed',
             'telegram_history_projection.token': token},
            {'$set': {
                'telegram_history_projection.state': 'synced',
                'telegram_history_projection.synced_at': datetime.now(timezone.utc),
                'telegram_history_projection.lease_until': None,
                'telegram_history_projection.error': None,
            }},
        )
        return result.matched_count == 1

    async def fail_history_projection(self, job_id, token, attempts, error):
        delay = bounded_backoff(
            attempts, runtime_config.bot.history_repair_retry_initial_seconds,
            runtime_config.bot.history_repair_backoff_coefficient,
            runtime_config.bot.history_repair_retry_max_seconds,
        )
        result = await asyncio.to_thread(
            self.jobs.update_one,
            {'_id': job_id, 'telegram_history_projection.state': 'claimed',
             'telegram_history_projection.token': token},
            {'$set': {
                'telegram_history_projection.state': 'pending',
                'telegram_history_projection.available_at': (
                    datetime.now(timezone.utc) + timedelta(seconds=delay)
                ),
                'telegram_history_projection.lease_until': None,
                'telegram_history_projection.error': str(error)[:runtime_config.bot.error_max_length],
            }},
        )
        return result.matched_count == 1

    def close(self):
        if self._mongo_client is not None:
            self._mongo_client.close()
