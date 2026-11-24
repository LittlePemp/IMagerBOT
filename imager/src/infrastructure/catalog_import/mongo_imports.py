"""Import inbox/outbox plus create-only, catalog-document publication fencing."""
import math
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from imager_config.loader import config
from imager_contracts import ObjectRef
from imager_contracts.catalog_import import CatalogImportRequest, import_fingerprint, make_import_submission
from src.application.catalog_import.errors import (
    CatalogImportBusy, CatalogImportConflict, InvalidCatalogArchive, LostCatalogImport,
)


class MongoImportRepository:
    def __init__(self, collection, catalog_collection, *, lease_seconds=None, clock=None):
        self.collection, self.catalogs = collection, catalog_collection
        self.lease_seconds = config.pack_import.lease_seconds if lease_seconds is None else lease_seconds
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def ensure_indexes(self):
        self.collection.create_index([('dispatch.state', 1), ('created_at', 1)])
        self.collection.create_index([('status', 1), ('updated_at', 1)])

    def get(self, import_id):
        return self.collection.find_one({'_id': import_id})

    find = get

    def receive(self, request):
        document = deepcopy(request) if isinstance(request, dict) and 'request' in request else make_import_submission(request, self.clock())
        snapshot = CatalogImportRequest.from_dict(document['request'])
        if document.get('_id') != snapshot.import_id or document.get('payload_hash') != import_fingerprint(snapshot):
            raise CatalogImportConflict('Параметры импорта не совпадают с его идентификатором.')
        try:
            self.collection.update_one({'_id': snapshot.import_id}, {'$setOnInsert': document}, upsert=True)
        except DuplicateKeyError:
            pass
        existing = self.get(snapshot.import_id)
        if existing is None or existing.get('payload_hash') != document['payload_hash']:
            raise CatalogImportConflict('Номер импорта уже использован с другими параметрами.')
        return existing

    def claim_dispatch(self):
        now, token = self.clock(), uuid4().hex
        job = self.collection.find_one_and_update(
            {'status': {'$nin': ['completed', 'failed']}, '$or': [
                {'dispatch.state': 'pending', 'dispatch.next_attempt_at': {'$lte': now}},
                {'dispatch.state': 'claimed', 'dispatch.lease_until': {'$lte': now}},
            ]},
            {'$set': {'dispatch.state': 'claimed', 'dispatch.token': token,
                      'dispatch.lease_until': now + timedelta(seconds=self.lease_seconds), 'updated_at': now},
             '$inc': {'dispatch.attempts': 1}},
            sort=[('created_at', 1)], return_document=ReturnDocument.AFTER,
        )
        if job is None:
            return None
        return {'import_id': job['_id'], 'request': job.get('request'), 'dispatch_token': token,
                'workflow_id': job.get('dispatch', {}).get('workflow_id'),
                'execution_policy': job.get('execution_policy')}

    def validate_dispatch(self, event):
        request = CatalogImportRequest.from_dict(event['request'])
        job = self.get(event['import_id'])
        if (job is None or request.import_id != event['import_id']
                or job.get('payload_hash') != import_fingerprint(request)
                or event['workflow_id'] != job['dispatch'].get('workflow_id')):
            raise CatalogImportConflict('Запрос импорта изменён после приёма.')
        policy = event.get('execution_policy')
        expected = {
            'import': {'activity_timeout_seconds', 'schedule_timeout_seconds', 'heartbeat_seconds',
                       'retry_initial_seconds', 'retry_max_seconds', 'maximum_attempts'},
            'failure': {'timeout_seconds', 'retry_initial_seconds', 'retry_max_seconds', 'maximum_attempts'},
        }
        if not isinstance(policy, dict) or set(policy) != set(expected) or policy != job.get('execution_policy'):
            raise CatalogImportConflict('Некорректная политика исполнения импорта.')
        for section, names in expected.items():
            values = policy[section]
            if not isinstance(values, dict) or set(values) != names:
                raise CatalogImportConflict('Некорректные параметры исполнения импорта.')
            for name, value in values.items():
                if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                    raise CatalogImportConflict('Параметры исполнения должны быть положительными.')
                if name == 'maximum_attempts' and type(value) is not int:
                    raise CatalogImportConflict('Количество попыток должно быть целым.')
            if values['retry_initial_seconds'] > values['retry_max_seconds']:
                raise CatalogImportConflict('Интервалы повторов некорректны.')
        execution = policy['import']
        if (execution['activity_timeout_seconds'] > execution['schedule_timeout_seconds']
                or execution['heartbeat_seconds'] >= execution['activity_timeout_seconds']):
            raise CatalogImportConflict('Тайм-ауты импорта некорректны.')
        return request.to_dict()

    def mark_dispatched(self, import_id, token, workflow_id):
        return self.collection.update_one(
            {'_id': import_id, 'dispatch.state': 'claimed', 'dispatch.token': token},
            {'$set': {'dispatch.state': 'sent', 'dispatch.workflow_id': workflow_id,
                      'dispatch.sent_at': self.clock(), 'updated_at': self.clock()}},
        ).matched_count == 1

    def defer_dispatch(self, import_id, token, error):
        return self.collection.update_one(
            {'_id': import_id, 'dispatch.state': 'claimed', 'dispatch.token': token},
            {'$set': {'dispatch.state': 'pending', 'dispatch.last_error': str(error)[:config.jobs.error_max_length],
                      'dispatch.next_attempt_at': self.clock() + timedelta(seconds=config.pack_import.dispatch_retry_seconds),
                      'updated_at': self.clock()}},
        ).matched_count == 1

    def reject_dispatch(self, import_id, token, error):
        return self.collection.update_one(
            {'_id': import_id, 'dispatch.state': 'claimed', 'dispatch.token': token},
            {'$set': {'dispatch.state': 'rejected', 'status': 'failed',
                      'error': str(error)[:config.jobs.error_max_length],
                      'public_error': 'Некорректный запрос импорта.', 'updated_at': self.clock()}},
        ).matched_count == 1

    @staticmethod
    def _result(catalog):
        return {name: catalog[name] for name in ('group_name', 'display_name', 'count', 'revision', 'manifest')}

    def _canonical(self, job):
        catalog = self.catalogs.find_one({'_id': job['request']['group_name']})
        if (catalog and catalog.get('count', 0) > 0 and catalog.get('manifest')
                and catalog.get('published_import_id') == job['_id']
                and catalog.get('published_payload_hash') == job['payload_hash']
                and catalog.get('published_manifest_sha256') == catalog.get('revision')):
            return self._result(catalog)
        return None

    def _job(self, import_id):
        job = self.get(import_id)
        if job is None:
            raise CatalogImportConflict('Неизвестный импорт.')
        return job

    def _catalog_claim(self, job, token, *, live=True):
        query = {'_id': job['request']['group_name'], 'count': 0, 'manifest': None,
                 'pending_import_id': job['_id'], 'publication_state': 'pending', 'publication_token': token}
        if live:
            query['publication_lease_until'] = {'$gt': self.clock()}
        return query

    def claim_execution(self, request):
        request = CatalogImportRequest.from_dict(request)
        job = self._job(request.import_id)
        if job.get('payload_hash') != import_fingerprint(request):
            raise CatalogImportConflict('Запрос не совпадает с принятым импортом.')
        canonical = job.get('result') if job['status'] == 'completed' else self._canonical(job)
        if canonical:
            self.collection.update_one({'_id': job['_id']}, {'$set': {
                'status': 'completed', 'result': canonical, 'updated_at': self.clock(),
                'progress': {'phase': 'completed', 'processed': canonical['count'], 'total': canonical['count']},
            }})
            return {'already_completed': True, 'result': canonical, 'token': None}
        if job['status'] == 'failed':
            raise CatalogImportConflict(job.get('public_error') or 'Импорт уже завершён с ошибкой.')
        now, token = self.clock(), uuid4().hex
        try:
            self.catalogs.update_one({'_id': request.group_name}, {'$setOnInsert': {
                'group': request.group_name, 'group_name': request.group_name,
                'name': request.group_name, 'display_name': request.display_name,
                'count': 0, 'manifest': None, 'pending_import_id': request.import_id,
                'publication_state': 'pending',
                'publication_token': None, 'publication_fence': 0, 'publication_lease_until': now,
            }}, upsert=True)
        except DuplicateKeyError:
            pass
        existing = self.catalogs.find_one({'_id': request.group_name})
        if (existing is None or existing.get('count', 0) > 0
                or existing.get('pending_import_id') != request.import_id
                or existing.get('publication_state') != 'pending'):
            raise CatalogImportConflict('Библиотека с таким идентификатором уже существует.')
        claimed = self.catalogs.find_one_and_update(
            {'_id': request.group_name, 'pending_import_id': request.import_id, 'count': 0, 'manifest': None,
             'publication_state': 'pending',
             '$or': [{'publication_token': None}, {'publication_lease_until': {'$lte': now}}]},
            {'$set': {'publication_token': token, 'publication_lease_until': now + timedelta(seconds=self.lease_seconds)},
             '$inc': {'publication_fence': 1}}, return_document=ReturnDocument.AFTER,
        )
        if claimed is None:
            raise CatalogImportBusy('Другой исполнитель уже обрабатывает этот импорт.')
        updated = self.collection.update_one(
            {'_id': request.import_id, 'payload_hash': job['payload_hash'], 'status': {'$nin': ['completed', 'failed']}},
            {'$set': {'status': 'running', 'execution.token': token,
                      'execution.fence': claimed['publication_fence'],
                      'execution.lease_until': now + timedelta(seconds=self.lease_seconds),
                      'progress': {'phase': 'downloading', 'processed': 0, 'total': 0}, 'updated_at': now}},
        )
        if updated.matched_count != 1:
            self.catalogs.update_one(self._catalog_claim(job, token, live=False), {'$set': {
                'publication_state': 'failed', 'publication_token': None, 'publication_lease_until': None,
            }})
            raise LostCatalogImport('Импорт уже завершён другим обработчиком.')
        return {'already_completed': False, 'token': token, 'fence': claimed['publication_fence']}

    def reserve_catalog(self, import_id, token, request=None):
        job = self._job(import_id)
        if self.catalogs.find_one(self._catalog_claim(job, token)) is None:
            raise LostCatalogImport('Исполнитель больше не владеет публикацией библиотеки.')
        return True

    def record_report(self, import_id, token, report):
        """Store the skipped-file report of the attempt that owns the import."""
        result = self.collection.update_one(
            {'_id': import_id, 'status': 'running', 'execution.token': token},
            {'$set': {'report': report, 'updated_at': self.clock()}},
        )
        if result.matched_count != 1:
            raise LostCatalogImport('Статус импорта изменён другим обработчиком.')

    def heartbeat_execution(self, import_id, token, progress=None):
        job = self._job(import_id)
        if self._canonical(job):
            return
        now = self.clock()
        result = self.catalogs.update_one(self._catalog_claim(job, token), {'$set': {
            'publication_lease_until': now + timedelta(seconds=self.lease_seconds),
        }})
        if result.matched_count != 1:
            raise LostCatalogImport('Срок владения импортом истёк или изменён.')
        fields = {'execution.lease_until': now + timedelta(seconds=self.lease_seconds), 'updated_at': now}
        if progress is not None:
            public_progress = {key: progress[key] for key in ('phase', 'processed', 'total') if key in progress}
            if public_progress:
                fields['progress'] = public_progress
        updated = self.collection.update_one(
            {'_id': import_id, 'status': 'running', 'execution.token': token}, {'$set': fields},
        )
        if updated.matched_count != 1:
            raise LostCatalogImport('Статус импорта изменён другим обработчиком.')

    def publish_catalog(self, import_id, token, result):
        job = self._job(import_id)
        canonical = self._canonical(job)
        if canonical:
            if canonical['revision'] != result['revision']:
                raise CatalogImportConflict('Импорт уже опубликован с другой ревизией.')
            return canonical
        manifest = ObjectRef.from_dict(result['manifest'])
        if (result['group_name'] != job['request']['group_name']
                or result['display_name'] != job['request']['display_name']
                or result['revision'] != manifest.sha256 or type(result['count']) is not int
                or not 0 < result['count'] <= config.catalog.max_images):
            raise CatalogImportConflict('Некорректный результат импорта.')
        document = dict(result, group=result['group_name'], name=result['group_name'],
                        published_import_id=import_id, published_payload_hash=job['payload_hash'],
                        published_manifest_sha256=result['revision'], updated_at=self.clock(),
                        publication_state='completed',
                        publication_token=None, publication_lease_until=None, pending_import_id=None)
        changed = self.catalogs.update_one(self._catalog_claim(job, token), {'$set': document})
        if changed.matched_count != 1:
            raise LostCatalogImport('Устаревший исполнитель не может опубликовать библиотеку.')
        return result

    def complete(self, import_id, token, result):
        job = self._job(import_id)
        canonical = self._canonical(job)
        if canonical is None or canonical['revision'] != result['revision']:
            raise LostCatalogImport('Публикация библиотеки не подтверждена.')
        changed = self.collection.update_one(
            {'_id': import_id, 'status': 'running', 'execution.token': token},
            {'$set': {'status': 'completed', 'result': canonical, 'error': None, 'public_error': None,
                      'updated_at': self.clock(), 'completed_at': self.clock(),
                      'progress': {'phase': 'completed', 'processed': canonical['count'], 'total': canonical['count']}}},
        )
        if changed.matched_count != 1:
            current = self._job(import_id)
            if current['status'] == 'completed':
                return current['result']
            raise LostCatalogImport('Подтверждение импорта принадлежит другому исполнителю.')
        return canonical

    @staticmethod
    def _public_error(error):
        if isinstance(error, (InvalidCatalogArchive, CatalogImportConflict)):
            return str(error)[:config.bot.error_max_length]
        if isinstance(error, MemoryError):
            return 'Для этого архива превышен допустимый бюджет памяти. Уменьшите пак.'
        return 'Импорт не удалось завершить. Повторите позже или проверьте архив.'

    def retry_execution(self, import_id, token, error):
        job = self._job(import_id)
        released = self.catalogs.update_one(self._catalog_claim(job, token), {'$set': {
            'publication_token': None, 'publication_lease_until': self.clock(),
        }})
        if released.matched_count != 1:
            return False
        return self.collection.update_one(
            {'_id': import_id, 'status': 'running', 'execution.token': token},
            {'$set': {'status': 'retrying', 'error': str(error)[:config.jobs.error_max_length], 'updated_at': self.clock()}},
        ).matched_count == 1

    def fail(self, import_id, token, error):
        job = self._job(import_id)
        canonical = self._canonical(job)
        if canonical:
            self.complete(import_id, token, canonical)
            return True
        fenced = self.catalogs.update_one(self._catalog_claim(job, token), {'$set': {
            'publication_state': 'failed', 'publication_token': None, 'publication_lease_until': None,
        }})
        if fenced.matched_count != 1:
            return False
        return self.collection.update_one(
            {'_id': import_id, 'status': 'running', 'execution.token': token},
            {'$set': {'status': 'failed', 'error': str(error)[:config.jobs.error_max_length],
                      'public_error': self._public_error(error), 'updated_at': self.clock()}},
        ).matched_count == 1

    def unfinished_jobs(self, limit=None):
        limit = config.temporal.dispatcher.reconcile_batch_size if limit is None else limit
        cursor = self.collection.find({'status': {'$nin': ['completed', 'failed']}, 'dispatch.state': 'sent'})
        jobs = list(cursor.sort([('reconcile_checked_at', 1), ('created_at', 1)]).limit(limit))
        for job in jobs:
            self.collection.update_one({'_id': job['_id']}, {'$set': {'reconcile_checked_at': self.clock()}})
        return [{'import_id': job['_id'], 'workflow_id': job['dispatch']['workflow_id']} for job in jobs]

    def finalize_failure(self, import_id, error):
        job = self._job(import_id)
        canonical = job.get('result') if job['status'] == 'completed' else self._canonical(job)
        if canonical:
            self.collection.update_one({'_id': import_id}, {'$set': {
                'status': 'completed', 'result': canonical, 'updated_at': self.clock(),
                'progress': {'phase': 'completed', 'processed': canonical['count'], 'total': canonical['count']},
            }})
            return {'status': 'completed', 'result': canonical}
        # The catalog document, not the separate job status, is the atomic
        # publication authority. Retain a terminal fence even before execution
        # began, so a late claimant that cached the old job cannot resurrect it.
        try:
            self.catalogs.update_one({'_id': job['request']['group_name']}, {'$setOnInsert': {
                'group': job['request']['group_name'], 'group_name': job['request']['group_name'],
                'name': job['request']['group_name'], 'display_name': job['request']['display_name'],
                'count': 0, 'manifest': None, 'pending_import_id': import_id,
                'publication_state': 'failed', 'publication_token': None, 'publication_lease_until': None,
            }}, upsert=True)
        except DuplicateKeyError:
            pass
        self.catalogs.update_one(
            {'_id': job['request']['group_name'], 'count': 0, 'manifest': None,
             'pending_import_id': import_id, 'publication_state': 'pending'},
            {'$set': {'publication_state': 'failed', 'publication_token': None, 'publication_lease_until': None}},
        )
        canonical = self._canonical(job)
        if canonical:
            self.collection.update_one({'_id': import_id}, {'$set': {
                'status': 'completed', 'result': canonical, 'updated_at': self.clock(),
                'progress': {'phase': 'completed', 'processed': canonical['count'], 'total': canonical['count']},
            }})
            return {'status': 'completed', 'result': canonical}
        self.collection.update_one({'_id': import_id, 'status': {'$nin': ['completed', 'failed']}}, {'$set': {
            'status': 'failed', 'error': str(error)[:config.jobs.error_max_length],
            'public_error': self._public_error(error), 'updated_at': self.clock(), 'execution.token': None,
        }})
        current = self._job(import_id)
        return {'status': current['status'], 'result': current.get('result')}
