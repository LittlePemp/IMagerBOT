"""Opt-in Mongo + MinIO smoke for StorageHygiene; uses its own random DB and bucket.

    python smoke_storage_hygiene.py mongodb://127.0.0.1:27199 127.0.0.1:9199 ACCESS SECRET
"""
import hashlib
import sys
import tempfile
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

CORE_ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(CORE_ROOT), str(CORE_ROOT.parent)]

from pymongo import MongoClient

from imager_config.loader import config
from src.infrastructure.jobs.storage_hygiene import StorageHygiene
from src.infrastructure.object_storage.minio_store import MinioObjectStore


def upload(store, directory, key, data):
    path = Path(directory) / uuid4().hex
    path.write_bytes(data)
    return store.upload_file(path, key, 'application/octet-stream')


def exists(store, key):
    return store.reference_for_key(key) is not None


def main(mongo_uri, endpoint, access, secret):
    client = MongoClient(mongo_uri, tz_aware=True, serverSelectionTimeoutMS=5000)
    database = client[f'hygiene_smoke_{uuid4().hex[:12]}']
    store = MinioObjectStore(endpoint, access, secret, f'hygiene-{uuid4().hex[:12]}', secure=False)
    store.ensure_bucket()
    now = datetime.now(timezone.utc)
    later = now + timedelta(days=2)
    old = now - timedelta(days=config.retention.job_document_days + 1)
    jobs, imports = database['jobs'], database['imports']
    try:
        with tempfile.TemporaryDirectory() as directory:
            # --- import archives ------------------------------------------------
            done = upload(store, directory, f'catalog/imports/imp_done/{hashlib.sha256(b"a").hexdigest()}.zip', b'zip-a')
            live = upload(store, directory, f'catalog/imports/imp_live/{hashlib.sha256(b"b").hexdigest()}.zip', b'zip-b')
            catalog = upload(store, directory, 'catalog/images/keep.png', b'catalog image')
            imports.insert_many([
                {'_id': 'imp_done', 'status': 'completed', 'updated_at': now, 'request': {'source': done}},
                {'_id': 'imp_live', 'status': 'running', 'updated_at': now, 'request': {'source': live}},
                {'_id': 'imp_foreign', 'status': 'failed', 'updated_at': now, 'request': {'source': catalog}},
            ])
            # --- results ----------------------------------------------------------
            canonical = upload(store, directory, 'jobs/job_done/attempts/tok1/result.jpg', b'canonical')
            lost = upload(store, directory, 'jobs/job_done/attempts/tok0/result.jpg', b'lost attempt')
            running = upload(store, directory, 'jobs/job_run/attempts/tok2/result.jpg', b'in progress')
            orphan = upload(store, directory, 'jobs/job_gone/attempts/tok3/result.jpg', b'no document')
            pinned_source = upload(store, directory, 'jobs/job_done/attempts/tok_shared/result.jpg', b'reused source')
            jobs.insert_many([
                {'_id': 'job_done', 'status': 'completed', 'updated_at': now, 'result': canonical},
                {'_id': 'job_run', 'status': 'running', 'updated_at': now},
                {'_id': 'source_consumer', 'status': 'running', 'updated_at': now,
                 'request': {'source': pinned_source}},
                # --- documents to purge or keep ----------------------------------
                {'_id': 'old_clean', 'status': 'completed', 'updated_at': old, 'request': {'source': {'key': 'x'}},
                 'result': {'key': 'y'}, 'artifact_lifecycle': {'source': {'state': 'deleted'},
                                                                'result': {'state': 'deleted'}}},
                {'_id': 'old_failed', 'status': 'failed', 'updated_at': old, 'request': {'source': {'key': 'x'}},
                 'artifact_lifecycle': {'source': {'state': 'deleted'}}},
                {'_id': 'old_with_result', 'status': 'completed', 'updated_at': old,
                 'request': {'source': {'key': 'x'}}, 'result': {'key': 'y'},
                 'artifact_lifecycle': {'source': {'state': 'deleted'}}},
                {'_id': 'old_undelivered', 'status': 'failed', 'updated_at': old, 'request': {'source': None},
                 'completion_outbox': {'state': 'pending'}},
                {'_id': 'recent_clean', 'status': 'failed', 'updated_at': now, 'request': {'source': None}},
                {'_id': 'old_unmanaged', 'status': 'failed', 'updated_at': old,
                 'request': {'source': {'key': 'unknown-layout/source'}},
                 'artifact_lifecycle': {'source': {'state': 'unmanaged'}}},
            ])
            for state in (None, 'pending', 'claimed', 'synced'):
                row = {
                    '_id': 'old_history_' + str(state), 'status': 'failed', 'updated_at': old,
                    'request': {'client_id': 'telegram', 'source': None,
                                'client_context': {'history_record_id': 'history-' + str(state)}},
                    'completion_outbox': {'state': 'sent'},
                }
                if state is not None:
                    row['telegram_history_projection'] = {'state': state}
                jobs.insert_one(row)

        policy = replace(config.retention, import_archive_grace_seconds=60, orphan_result_grace_seconds=60)
        hygiene = StorageHygiene(jobs, imports, store, policy=policy, clock=lambda: later)
        report = hygiene.cleanup_once()
        assert report == {'import_archives': 1, 'orphan_results': 2, 'job_documents': 3}, report

        assert not exists(store, done['key']) and exists(store, live['key']) and exists(store, catalog['key'])
        assert imports.find_one({'_id': 'imp_done'})['archive_lifecycle']['state'] == 'deleted'
        assert imports.find_one({'_id': 'imp_foreign'})['archive_lifecycle']['state'] == 'unmanaged'
        assert exists(store, canonical['key']) and exists(store, running['key']) and exists(store, pinned_source['key'])
        assert not exists(store, lost['key']) and not exists(store, orphan['key'])
        remaining = {row['_id'] for row in jobs.find({}, {'_id': 1})}
        assert remaining == {
            'job_done', 'job_run', 'source_consumer', 'old_with_result', 'old_undelivered',
            'recent_clean', 'old_unmanaged', 'old_history_None', 'old_history_pending', 'old_history_claimed',
        }, remaining

        # Idempotent: a second pass finds nothing more to do.
        assert hygiene.cleanup_once() == {'import_archives': 0, 'orphan_results': 0, 'job_documents': 0}
        # Grace period: nothing is removed when "now" is the upload time.
        fresh = StorageHygiene(jobs, imports, store, policy=policy, clock=lambda: now - timedelta(minutes=5))
        assert fresh.cleanup_orphan_results() == 0
        print('storage hygiene smoke: OK')
    finally:
        for key, _ in store.list_objects('catalog/'):
            store.client.remove_object(store.bucket, key)
        for key, _ in store.list_objects('jobs/'):
            store.client.remove_object(store.bucket, key)
        store.client.remove_bucket(store.bucket)
        client.drop_database(database.name)
        client.close()


if __name__ == '__main__':
    main(*sys.argv[1:5])
