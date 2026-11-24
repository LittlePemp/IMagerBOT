"""Removal of storage the generation retention cannot see.

ImageRetention keeps the latest generation artifacts that job documents point
at. Three kinds of data were never released:

* import ZIPs (up to ``pack_import.max_archive_bytes`` each) after the import
  reached a terminal state;
* result objects uploaded by an attempt that then lost its execution lease, or
  whose process died between upload and completion: no document references
  them;
* terminal job documents themselves, after their artifacts are gone.

Every deletion is verified against the owning document at the moment of
deletion. Catalog images and manifests are never touched.
"""
import asyncio
import re
from contextlib import suppress
from datetime import datetime, timedelta, timezone

from imager_config.loader import config
from imager_contracts.protocol import JOB_ID_PATTERN
from src.shared_kernel.loggers import presentation_logger

TERMINAL = ('completed', 'failed')
DELIVERING = ('pending', 'preparing', 'claimed')
GONE = ('deleted', 'unmanaged')


def _prefix(template, **values):
    """Literal key prefix of a template up to its first unresolved field."""
    head = template
    for name, value in values.items():
        head = head.replace('{' + name + '}', value)
    return head.split('{', 1)[0]


def archive_prefix(import_id):
    return _prefix(config.pack_import.archive_key_template, import_id=import_id)


def result_job_id(key):
    """Job identity of a key in the result layout, or None for any other key."""
    template = config.storage.result_key_template
    pattern = re.escape(template)
    for name, expression in (('job_id', f'(?P<job_id>{JOB_ID_PATTERN.strip("^$")})'),
                             ('execution_token', f'(?:{JOB_ID_PATTERN.strip("^$")})'),
                             ('result_filename', re.escape(config.storage.result_filename))):
        pattern = pattern.replace(re.escape('{' + name + '}'), expression)
    match = re.fullmatch(pattern, key or '')
    return match['job_id'] if match else None


class StorageHygiene:
    def __init__(self, jobs, imports, store, *, policy=None, clock=None):
        self.jobs, self.imports, self.store = jobs, imports, store
        self.policy = config.retention if policy is None else policy
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    # --- import archives -------------------------------------------------
    def cleanup_import_archives(self):
        cutoff = self.clock() - timedelta(seconds=self.policy.import_archive_grace_seconds)
        rows = list(self.imports.find({
            'status': {'$in': list(TERMINAL)}, 'updated_at': {'$lte': cutoff},
            'archive_lifecycle.state': {'$nin': list(GONE)},
        }).sort([('updated_at', 1)]).limit(self.policy.batch_size))
        removed = 0
        for row in rows:
            reference = (row.get('request') or {}).get('source') or {}
            key = reference.get('key')
            if (reference.get('bucket') != self.store.bucket or not isinstance(key, str)
                    or not key.startswith(archive_prefix(row['_id']))):
                self._mark_archive(row['_id'], 'unmanaged', reason='archive outside the import layout')
                continue
            try:
                self.store.delete_file(reference)
            except Exception as error:
                presentation_logger.exception(f'Import archive deletion failed: import_id={row["_id"]}')
                self._mark_archive(row['_id'], 'pending', last_error=str(error)[:config.jobs.error_max_length])
                continue
            if self._mark_archive(row['_id'], 'deleted'):
                removed += 1
        return removed

    def _mark_archive(self, import_id, state, **values):
        changes = {'archive_lifecycle.state': state, 'archive_lifecycle.updated_at': self.clock(),
                   **{f'archive_lifecycle.{name}': value for name, value in values.items()}}
        # Status stays terminal: only a finished import can lose its archive.
        result = self.imports.update_one(
            {'_id': import_id, 'status': {'$in': list(TERMINAL)}}, {'$set': changes},
        )
        return result.matched_count == 1

    # --- orphan results --------------------------------------------------
    def cleanup_orphan_results(self, limit=None):
        limit = self.policy.batch_size if limit is None else limit
        cutoff = self.clock() - timedelta(seconds=self.policy.orphan_result_grace_seconds)
        prefix = _prefix(config.storage.result_key_template)
        removed = 0
        for key, modified in self.store.list_objects(prefix):
            if removed >= limit:
                break
            if modified is None or modified > cutoff:
                continue
            job_id = result_job_id(key)
            if job_id is None:
                continue
            job = self.jobs.find_one({'_id': job_id}, {'status': 1, 'result': 1})
            if job is not None and job.get('status') not in TERMINAL:
                continue  # a live attempt may still publish this object
            if job is not None and (job.get('result') or {}).get('key') == key:
                continue  # the canonical result: ImageRetention owns it
            reference = self.store.reference_for_key(key)
            if reference is None:
                continue
            self.store.delete_file(reference)
            removed += 1
            presentation_logger.info(f'Removed unreferenced result: job_id={job_id}')
        return removed

    # --- job documents ---------------------------------------------------
    def purge_job_documents(self):
        cutoff = self.clock() - timedelta(days=self.policy.job_document_days)
        result = self.jobs.delete_many({
            'status': {'$in': list(TERMINAL)}, 'updated_at': {'$lt': cutoff},
            'completion_outbox.state': {'$nin': list(DELIVERING)},
            '$and': [
                {'$or': [{'request.source': None},
                         {'artifact_lifecycle.source.state': {'$in': list(GONE)}}]},
                {'$or': [{'result': None},
                         {'artifact_lifecycle.result.state': {'$in': list(GONE)}}]},
            ],
        })
        return result.deleted_count

    def cleanup_once(self):
        report = {}
        for name, step in (('import_archives', self.cleanup_import_archives),
                           ('orphan_results', self.cleanup_orphan_results),
                           ('job_documents', self.purge_job_documents)):
            try:
                report[name] = step()
            except Exception:
                presentation_logger.exception(f'Storage hygiene step failed: {name}')
        return report

    async def run(self, stop):
        while not stop.is_set():
            report = await asyncio.to_thread(self.cleanup_once)
            if any(report.values()):
                presentation_logger.info(f'Storage hygiene removed: {report}')
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=self.policy.hygiene_poll_seconds)


__all__ = ['StorageHygiene', 'archive_prefix', 'result_job_id']
