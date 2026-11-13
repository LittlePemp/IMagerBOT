"""Bounded retention of accepted generation artifacts, never catalog assets."""
import asyncio
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from pymongo import ReturnDocument

from imager_config.loader import config
from imager_contracts import ObjectRef
from src.shared_kernel.loggers import presentation_logger


ARTIFACTS = {
    'source': ('request.source', 'created_at', 'source_count'),
    'result': ('result', 'completed_at', 'result_count'),
}
TERMINAL = ('completed', 'failed')
DELIVERING = ('pending', 'preparing', 'claimed')


def _value(document, path):
    value = document
    for part in path.split('.'):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


class ImageRetention:
    def __init__(self, collection, object_store, *, policy=None, clock=None):
        self.collection, self.object_store = collection, object_store
        self.policy = config.retention if policy is None else policy
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._after = {kind: None for kind in ARTIFACTS}

    def initialize_indexes(self):
        self.collection.create_index([('created_at', -1), ('_id', -1)])
        self.collection.create_index([('completed_at', -1), ('_id', -1)])
        for kind, (field, _, _) in ARTIFACTS.items():
            self.collection.create_index([(field + '.bucket', 1), (field + '.key', 1)])

    @staticmethod
    def _order(document, time_field):
        timestamp = document.get(time_field) or document.get('created_at')
        if not isinstance(timestamp, datetime):
            timestamp = datetime.min.replace(tzinfo=timezone.utc)
        elif timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        return timestamp, document['_id']

    @staticmethod
    def _identity(reference):
        return reference['bucket'], reference['key']

    def _reference(self, document, kind):
        field = ARTIFACTS[kind][0]
        ref = ObjectRef.from_dict(_value(document, field)).to_dict()
        key, job_id = ref['key'], document['_id']
        # Prefixes retained from older catalog layouts are protected too.
        protected = ['catalog/'] + [template.split('{', 1)[0] for template in (
            config.storage.catalog_image_key_template,
            config.storage.catalog_manifest_key_template,
            config.pack_import.archive_key_template,
        )]
        if any(not prefix or key.startswith(prefix) for prefix in protected):
            raise ValueError('Generation artifact points into a catalog/import namespace')
        if kind == 'source':
            expected = config.storage.source_key_template.format(job_id=job_id, sha256=ref['sha256'])
            # Accepted historical uploads used the configured source filename.
            legacy = {f'uploads/{job_id}/{name}' for name in (
                config.storage.source_filename, config.storage.upload_filename,
            )}
        else:
            token = (document.get('execution') or {}).get('token')
            if not token:
                raise ValueError('Result has no owning execution token')
            expected = config.storage.result_key_template.format(
                job_id=job_id, execution_token=token,
                result_filename=config.storage.result_filename,
            )
            legacy = set()
        if key != expected and key not in legacy:
            raise ValueError('Artifact is not owned by this job under a recognized storage layout')
        if ref['bucket'] != self.object_store.bucket:
            raise ValueError('Artifact uses a different bucket')
        return ref

    @staticmethod
    def _protected(document, kind):
        if document.get('status') not in TERMINAL:
            return True
        return (kind == 'result' and _value(document, 'request.client_id') == 'telegram'
                and _value(document, 'completion_outbox.state') in DELIVERING)

    def _latest(self, kind):
        field, timestamp, setting = ARTIFACTS[kind]
        count = getattr(self.policy, setting)
        query = {field: {'$ne': None}, f'artifact_lifecycle.{kind}.state': {'$ne': 'deleted'}}
        rows = list(self.collection.find(query).sort([(timestamp, -1), ('_id', -1)])
                    .limit(self.policy.scan_size))
        keys, boundary = set(), None
        for row in rows:
            try:
                reference = self._reference(row, kind)
            except (ValueError, TypeError):
                continue
            keys.add(self._identity(reference))
            boundary = self._order(row, timestamp)
            if len(keys) >= count:
                return keys, boundary
        # A malformed/shared-prefix flood must never cause a guessed rank.
        return keys, None

    def _page(self, kind):
        field = ARTIFACTS[kind][0]
        query = {field: {'$ne': None},
                 f'artifact_lifecycle.{kind}.state': {'$nin': ['deleted', 'unmanaged']}}
        if self._after[kind] is not None:
            query['_id'] = {'$gt': self._after[kind]}
        rows = list(self.collection.find(query).sort([('_id', 1)]).limit(self.policy.batch_size))
        if not rows and self._after[kind] is not None:
            self._after[kind] = None
            query.pop('_id')
            rows = list(self.collection.find(query).sort([('_id', 1)]).limit(self.policy.batch_size))
        if rows:
            self._after[kind] = rows[-1]['_id']
        return rows

    def _shared(self, kind, reference, owner_id):
        # Shared keys are conservatively retained, even if both jobs completed.
        # Normal uploads are job-bound; arbitrary external reuse needs a writer
        # pin protocol before it could be reclaimed safely across documents.
        rows = list(self.collection.find({'$or': [
            {field + '.bucket': reference['bucket'], field + '.key': reference['key']}
            for field, _, _ in ARTIFACTS.values()
        ]}).limit(2))
        if len(rows) != 1 or rows[0]['_id'] != owner_id:
            return True
        other_kind = 'result' if kind == 'source' else 'source'
        other = _value(rows[0], ARTIFACTS[other_kind][0])
        return bool(other and (other.get('bucket'), other.get('key')) == self._identity(reference))

    def _claim(self, document, kind, reference):
        field = ARTIFACTS[kind][0]
        state = f'artifact_lifecycle.{kind}'
        now, token = self.clock(), uuid4().hex
        query = {
            '_id': document['_id'], 'status': {'$in': list(TERMINAL)},
            field + '.bucket': reference['bucket'], field + '.key': reference['key'],
            field + '.sha256': reference['sha256'],
            '$or': [
                {state + '.state': {'$exists': False}},
                {state + '.state': 'pending', state + '.next_attempt_at': {'$lte': now}},
                {state + '.state': 'deleting', state + '.lease_until': {'$lte': now}},
            ],
        }
        if kind == 'result':
            query['$nor'] = [{'request.client_id': 'telegram',
                              'completion_outbox.state': {'$in': list(DELIVERING)}}]
        claimed = self.collection.find_one_and_update(query, {'$set': {
            state + '.state': 'deleting', state + '.token': token,
            state + '.lease_until': now + timedelta(seconds=self.policy.lease_seconds),
        }}, return_document=ReturnDocument.AFTER)
        return (claimed, token) if claimed else (None, None)

    def _remember(self, job_id, kind, token, state, **values):
        prefix = f'artifact_lifecycle.{kind}'
        changes = {prefix + '.state': state, prefix + '.token': None, **{
            prefix + '.' + key: value for key, value in values.items()
        }}
        self.collection.update_one({'_id': job_id, prefix + '.token': token}, {'$set': changes})

    def cleanup_once(self, *, dry_run=False):
        report = {kind: 0 for kind in ARTIFACTS}
        if not self.policy.enabled:
            return report
        for kind, (_, timestamp, _) in ARTIFACTS.items():
            keep, boundary = self._latest(kind)
            if boundary is None:
                continue
            for row in self._page(kind):
                if self._protected(row, kind) or self._order(row, timestamp) >= boundary:
                    continue
                try:
                    reference = self._reference(row, kind)
                except (ValueError, TypeError) as error:
                    if not dry_run:
                        self.collection.update_one({'_id': row['_id']}, {'$set': {
                            f'artifact_lifecycle.{kind}.state': 'unmanaged',
                            f'artifact_lifecycle.{kind}.reason': str(error),
                        }})
                        presentation_logger.warning(f'Retention skipped unmanaged {kind}: job_id={row["_id"]}')
                    continue
                if self._identity(reference) in keep or self._shared(kind, reference, row['_id']):
                    continue
                if dry_run:
                    report[kind] += 1
                    continue
                claimed, token = self._claim(row, kind, reference)
                if claimed is None:
                    continue
                try:
                    # Recheck after the CAS: delivery/ownership may have changed
                    # while this document was being selected.
                    current = self.collection.find_one({'_id': row['_id']})
                    if (current is None or self._protected(current, kind)
                            or self._reference(current, kind) != reference
                            or self._shared(kind, reference, row['_id'])):
                        self._remember(row['_id'], kind, token, 'pending',
                                       next_attempt_at=self.clock() + timedelta(seconds=self.policy.poll_seconds))
                        continue
                    self.object_store.delete_file(reference)
                    self._remember(row['_id'], kind, token, 'deleted', deleted_at=self.clock())
                    report[kind] += 1
                except Exception as error:
                    presentation_logger.exception(f'Retention deletion failed: kind={kind}, job_id={row["_id"]}')
                    self._remember(row['_id'], kind, token, 'pending',
                                   next_attempt_at=self.clock() + timedelta(seconds=self.policy.poll_seconds),
                                   last_error=str(error)[:config.jobs.error_max_length])
        return report

    async def run(self, stop):
        while not stop.is_set():
            try:
                await asyncio.to_thread(self.cleanup_once)
            except asyncio.CancelledError:
                raise
            except Exception:
                presentation_logger.exception('Generation artifact retention failed')
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=self.policy.poll_seconds)
