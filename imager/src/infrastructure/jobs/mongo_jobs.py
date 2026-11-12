from datetime import datetime, timedelta, timezone
from dataclasses import asdict
import math
from uuid import uuid4

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from imager_contracts import JobRequest, ObjectRef, make_submission, payload_fingerprint
from imager_config.loader import config


class JobConflict(ValueError):
    pass


class JobBusy(RuntimeError):
    retryable = True


class LostExecution(RuntimeError):
    retryable = True


class MongoJobRepository:
    """One document is the inbox, execution state and transactional outbox.

    Every transition is a single Mongo compare-and-set. No distributed or
    multi-document transaction is implied, and no file is a shared job state.
    """

    def __init__(self, collection, *, lease_seconds=None, clock=None):
        self.collection = collection
        self.lease_seconds = config.jobs.execution_lease_seconds if lease_seconds is None else lease_seconds
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def ensure_indexes(self):
        self.collection.create_index([('dispatch.state', 1), ('created_at', 1)])
        self.collection.create_index([
            ('request.client_id', 1), ('completion_outbox.state', 1),
            ('completion_outbox.lease_until', 1),
        ])

    def receive(self, request):
        document = make_submission(request, self.clock())
        try:
            self.collection.update_one(
                {'_id': document['_id']}, {'$setOnInsert': document}, upsert=True,
            )
        except DuplicateKeyError:
            pass
        existing = self.collection.find_one({'_id': document['_id']})
        if existing is None or existing.get('payload_hash') != document['payload_hash']:
            raise JobConflict('Job ID was already used with different input or parameters')
        return existing

    def claim_dispatch(self):
        now, token = self.clock(), uuid4().hex
        job = self.collection.find_one_and_update(
            {'$or': [
                {'dispatch.state': 'pending', 'dispatch.next_attempt_at': {'$lte': now}},
                {'dispatch.state': 'claimed', 'dispatch.lease_until': {'$lte': now}},
            ]},
            {'$set': {
                'dispatch.state': 'claimed', 'dispatch.token': token,
                'dispatch.lease_until': now + timedelta(seconds=self.lease_seconds),
                'updated_at': now,
            }, '$inc': {'dispatch.attempts': 1}},
            sort=[('created_at', 1), ('_id', 1)], return_document=ReturnDocument.AFTER,
        )
        if job is None:
            return None
        if 'execution_policy' not in job:
            # Upgrade a legacy inbox once, before Temporal can accept its start.
            # A retry after config changes must reuse the same policy snapshot.
            self.collection.update_one(
                {'_id': job['_id'], 'execution_policy': {'$exists': False}},
                {'$set': {'execution_policy': {
                    'render': asdict(config.temporal.render),
                    'failure': asdict(config.temporal.failure),
                }}},
            )
            job = self.collection.find_one({'_id': job['_id']})
        workflow_id = job.get('dispatch', {}).get('workflow_id')
        if not workflow_id:
            workflow_id = f'{config.temporal.workflow_id_prefix}{job["_id"]}'
            self.collection.update_one(
                {'_id': job['_id'], 'dispatch.workflow_id': None},
                {'$set': {'dispatch.workflow_id': workflow_id}},
            )
            job = self.collection.find_one({'_id': job['_id']})
            workflow_id = job['dispatch']['workflow_id']
        return {'job_id': job['_id'], 'request': job.get('request'),
                'dispatch_token': token, 'attempts': job['dispatch']['attempts'],
                'execution_policy': job['execution_policy'], 'workflow_id': workflow_id}

    def mark_dispatched(self, job_id, token, workflow_id):
        result = self.collection.update_one(
            {'_id': job_id, 'dispatch.state': 'claimed', 'dispatch.token': token},
            {'$set': {'dispatch.state': 'sent', 'dispatch.workflow_id': workflow_id,
                      'dispatch.sent_at': self.clock(), 'updated_at': self.clock()}},
        )
        return result.matched_count == 1

    def validate_dispatch(self, event):
        snapshot = JobRequest.from_dict(event['request']).to_dict()
        job = self.collection.find_one({'_id': event['job_id']})
        if (job is None or snapshot['job_id'] != event['job_id']
                or job.get('payload_hash') != payload_fingerprint(snapshot)):
            raise JobConflict('Inbox payload was modified after submission')
        policy = event.get('execution_policy')
        if not isinstance(policy, dict) or set(policy) != {'render', 'failure'}:
            raise JobConflict('Invalid execution policy snapshot')
        for name, defaults in (('render', config.temporal.render), ('failure', config.temporal.failure)):
            values = policy[name]
            if not isinstance(values, dict) or set(values) != set(asdict(defaults)):
                raise JobConflict('Invalid execution policy fields')
            for field, value in values.items():
                if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                    raise JobConflict('Execution policy values must be finite and positive')
                if field == 'maximum_attempts' and type(value) is not int:
                    raise JobConflict('Execution retry count must be an integer')
            if (values['retry_initial_seconds'] > values['retry_maximum_seconds']
                    or values['start_to_close_seconds'] > values['schedule_to_close_seconds']):
                raise JobConflict('Execution policy intervals are inverted')
        if (policy != job.get('execution_policy')
                or event.get('workflow_id') != job.get('dispatch', {}).get('workflow_id')):
            raise JobConflict('Dispatch policy or workflow identity changed after claiming')
        return snapshot

    def unfinished_jobs(self, limit=None):
        limit = config.temporal.dispatcher.reconcile_batch_size if limit is None else limit
        jobs = list(self.collection.find({
            'status': {'$in': ['queued', 'running', 'retrying']},
            'dispatch.state': 'sent',
        }).sort([('reconcile_checked_at', 1), ('created_at', 1)]).limit(limit))
        for job in jobs:
            self.collection.update_one(
                {'_id': job['_id']}, {'$set': {'reconcile_checked_at': self.clock()}},
            )
        return [{'job_id': job['_id'],
                 'workflow_id': job.get('dispatch', {}).get('workflow_id',
                     f'{config.temporal.workflow_id_prefix}{job["_id"]}')}
                for job in jobs]

    def reject_dispatch(self, job_id, token, error):
        result = self.collection.update_one(
            {'_id': job_id, 'dispatch.state': 'claimed', 'dispatch.token': token},
            {'$set': {'dispatch.state': 'rejected', 'status': 'failed',
                      'error': str(error)[:config.jobs.error_max_length], 'updated_at': self.clock(),
                      'completion_outbox': self._completion_event(job_id, 'failed')}},
        )
        return result.matched_count == 1

    def defer_dispatch(self, job_id, token, error):
        now = self.clock()
        result = self.collection.update_one(
            {'_id': job_id, 'dispatch.state': 'claimed', 'dispatch.token': token},
            {'$set': {'dispatch.state': 'pending', 'dispatch.last_error': str(error)[:config.jobs.error_max_length],
                      'dispatch.next_attempt_at': now + timedelta(seconds=config.jobs.dispatch_retry_seconds), 'updated_at': now}},
        )
        return result.matched_count == 1

    def claim_execution(self, request):
        snapshot = JobRequest.from_dict(request).to_dict()
        job_id, fingerprint = snapshot['job_id'], payload_fingerprint(snapshot)
        existing = self.collection.find_one({'_id': job_id})
        if existing is None or existing.get('payload_hash') != fingerprint:
            raise JobConflict('Workflow input does not match its accepted inbox record')
        if existing['status'] == 'completed':
            return {'already_completed': True, 'result': existing['result'], 'token': None}
        if existing['status'] == 'failed':
            raise ValueError(existing.get('error') or 'Job already failed permanently')
        now, token = self.clock(), uuid4().hex
        claimed = self.collection.find_one_and_update(
            {'_id': job_id, 'payload_hash': fingerprint, '$or': [
                {'status': {'$in': ['queued', 'retrying']}},
                {'status': 'running', 'execution.lease_until': {'$lte': now}},
            ]},
            {'$set': {'status': 'running', 'execution.token': token,
                      'execution.lease_until': now + timedelta(seconds=self.lease_seconds),
                      'execution.started_at': now, 'updated_at': now},
             '$inc': {'execution.fence': 1}},
            return_document=ReturnDocument.AFTER,
        )
        if claimed is None:
            # Recheck completion after a competing transition, before retrying.
            current = self.collection.find_one({'_id': job_id})
            if current and current['status'] == 'completed':
                return {'already_completed': True, 'result': current['result'], 'token': None}
            raise JobBusy('A live attempt already owns this job')
        return {'already_completed': False, 'token': token,
                'fence': claimed['execution']['fence']}

    def heartbeat_execution(self, job_id, token, progress=None):
        now = self.clock()
        updates = {'execution.lease_until': now + timedelta(seconds=self.lease_seconds),
                   'updated_at': now}
        if progress is not None:
            updates['execution.progress'] = progress
        result = self.collection.update_one(
            {'_id': job_id, 'status': 'running', 'execution.token': token},
            {'$set': updates},
        )
        if result.matched_count != 1:
            raise LostExecution('Execution lease was replaced or the job already finished')

    def _completion_event(self, job_id, status):
        now = self.clock()
        return {'event_id': f'{job_id}:{status}', 'state': 'pending', 'attempts': 0,
                'token': None, 'lease_until': now, 'available_at': now, 'created_at': now}

    def complete(self, job_id, token, result_ref):
        reference = ObjectRef.from_dict(result_ref).to_dict()
        result = self.collection.update_one(
            {'_id': job_id, 'status': 'running', 'execution.token': token},
            {'$set': {'status': 'completed', 'result': reference, 'updated_at': self.clock(),
                      'completed_at': self.clock(),
                      'completion_outbox': self._completion_event(job_id, 'completed')}},
        )
        if result.matched_count != 1:
            raise LostExecution('Stale execution cannot publish a result')
        return reference

    def retry_execution(self, job_id, token, error):
        result = self.collection.update_one(
            {'_id': job_id, 'status': 'running', 'execution.token': token},
            {'$set': {'status': 'retrying', 'error': str(error)[:config.jobs.error_max_length],
                      'execution.lease_until': self.clock(), 'updated_at': self.clock()}},
        )
        return result.matched_count == 1

    def fail(self, job_id, token, error):
        result = self.collection.update_one(
            {'_id': job_id, 'status': 'running', 'execution.token': token},
            {'$set': {'status': 'failed', 'error': str(error)[:config.jobs.error_max_length],
                      'updated_at': self.clock(), 'completed_at': self.clock(),
                      'completion_outbox': self._completion_event(job_id, 'failed')}},
        )
        return result.matched_count == 1

    def finalize_failure(self, job_id, error):
        """Workflow compensation for worker death/timeouts outside Python code."""
        self.collection.update_one(
            {'_id': job_id, 'status': {'$nin': ['completed', 'failed']}},
            {'$set': {'status': 'failed', 'error': str(error)[:config.jobs.error_max_length],
                      'execution.token': None, 'updated_at': self.clock(),
                      'completion_outbox': self._completion_event(job_id, 'failed')}},
        )
        current = self.collection.find_one({'_id': job_id})
        if current is None:
            raise JobConflict('Unknown workflow job')
        return {'status': current['status'], 'result': current.get('result')}
