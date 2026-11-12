"""Opt-in real Temporal/Mongo/MinIO import test; no Telegram or in-process worker.

Run in the separate catalog-import.compose.yaml fixture after building core.
The controller only submits contracts and dispatches outbox records. The actual
Activity must run in the dedicated worker container with the expected identity.
"""
import asyncio
import json
import os
import sys
import tempfile
import zipfile
from pathlib import Path
from uuid import uuid4

CORE_ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(CORE_ROOT), str(CORE_ROOT.parent)]

from imager_config.loader import config
from imager_contracts import ObjectRef
from imager_contracts.catalog_import import CatalogImportRequest, ZIP_CONTENT_TYPE
from settings import settings
from src.infrastructure.catalog_import.bootstrap import create_import_repository
from src.infrastructure.object_storage.minio_store import file_sha256, object_store_from_settings
from src.infrastructure.temporal_client import connect_temporal_client
from src.presentation.temporal.dispatcher import dispatch_import_once


def assert_isolated_fixture():
    if (os.environ.get('IMAGER_PACK_SMOKE_ALLOW') != 'isolated-fixture'
            or settings.database_name != 'imager_pack_smoke'
            or settings.minio_bucket != 'imager-pack-smoke'
            or settings.temporal_namespace != 'imager-pack-smoke'):
        raise RuntimeError('Use only the isolated catalog-import.compose.yaml fixture')


def make_large_archive(directory):
    import cv2
    import numpy as np

    archive = directory / config.pack_import.archive_filename
    rng = np.random.default_rng(481516)
    with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_STORED) as packed:
        for index in range(8):
            pixels = rng.integers(0, 256, (1024, 1024, 3), dtype=np.uint8)
            success, encoded = cv2.imencode('.png', pixels)
            if not success:
                raise RuntimeError('Cannot encode synthetic test PNG')
            packed.writestr(f'icons/icon-{index}.png', encoded.tobytes())
            del pixels, encoded
    if not config.pack_import.cloud_max_archive_bytes < archive.stat().st_size <= config.pack_import.max_archive_bytes:
        raise AssertionError('The fixture must exceed the cloud download limit')
    return archive


def prepare_import(path, store, label):
    import_id = uuid4().hex
    source = store.upload_file(
        path,
        config.pack_import.archive_key_template.format(import_id=import_id, sha256=file_sha256(path)),
        ZIP_CONTENT_TYPE,
    )
    return CatalogImportRequest(
        import_id=import_id, actor_id=777,
        group_name=f'pack_smoke_{import_id}', display_name=label,
        source=ObjectRef.from_dict(source),
    )


async def wait_for_workflow(client, workflow_id, *, expect_failure=False):
    from temporalio.client import WorkflowFailureError

    handle = client.get_workflow_handle(workflow_id)
    try:
        result = await asyncio.wait_for(handle.result(), timeout=240)
    except WorkflowFailureError:
        if not expect_failure:
            raise
        result = None
    else:
        if expect_failure:
            raise AssertionError('Unsafe archive unexpectedly completed')
    history = await handle.fetch_history()
    started = [event.activity_task_started_event_attributes for event in history.events
               if event.HasField('activity_task_started_event_attributes')]
    expected = os.environ['IMAGER_PACK_SMOKE_IDENTITY']
    if not started or any(event.identity != expected for event in started):
        raise AssertionError('Activities did not execute in the dedicated import worker')
    return result, [event.identity for event in started]


async def run():
    assert_isolated_fixture()
    store = object_store_from_settings(settings)
    store.ensure_bucket()
    repository = create_import_repository()
    client = await connect_temporal_client(settings)
    with tempfile.TemporaryDirectory(prefix='imager-pack-smoke-') as temporary:
        directory = Path(temporary)
        archive = make_large_archive(directory)
        request = prepare_import(archive, store, 'Проверочный пак больше 20 MB')
        accepted = repository.receive(request)
        if not await dispatch_import_once(repository, client):
            raise AssertionError('Import start-outbox was not dispatched')
        result, identities = await wait_for_workflow(client, accepted['dispatch']['workflow_id'])
        job = repository.get(request.import_id)
        catalog = repository.catalogs.find_one({'_id': request.group_name})
        if (job['status'] != 'completed' or result != job['result'] or catalog['count'] != 8
                or catalog['display_name'] != request.display_name
                or job['progress']['phase'] != 'completed'
                or job['progress']['processed'] != 8):
            raise AssertionError('Import status, progress or published catalog is inconsistent')
        manifest = store.read_json(job['result']['manifest'])
        if len(manifest['images']) != 8:
            raise AssertionError('Manifest is incomplete')
        for image in manifest['images']:
            ref = image['object']
            stored = store.client.stat_object(ref['bucket'], ref['key'])
            if stored.size != ref['size_bytes']:
                raise AssertionError('A manifest image is missing from MinIO')
        duplicate = repository.receive(request)
        if (duplicate['status'] != 'completed' or duplicate['result'] != job['result']
                or duplicate['execution']['fence'] != job['execution']['fence']
                or await dispatch_import_once(repository, client)):
            raise AssertionError('Duplicate import was not idempotent')

        bad_zip = directory / 'unsafe.zip'
        with zipfile.ZipFile(bad_zip, 'w', compression=zipfile.ZIP_STORED) as packed:
            packed.writestr('../escape.png', b'not-an-image')
        unsafe = prepare_import(bad_zip, store, 'Недопустимый путь')
        bad_accepted = repository.receive(unsafe)
        if not await dispatch_import_once(repository, client):
            raise AssertionError('Unsafe import was not dispatched for validation')
        await wait_for_workflow(client, bad_accepted['dispatch']['workflow_id'], expect_failure=True)
        bad_job = repository.get(unsafe.import_id)
        leaked = repository.catalogs.find_one({'_id': unsafe.group_name, 'count': {'$gt': 0}})
        if bad_job['status'] != 'failed' or leaked is not None:
            raise AssertionError('Unsafe archive published a catalog or lost failure status')
        print(json.dumps({
            'status': 'passed', 'archive_bytes': archive.stat().st_size,
            'images': catalog['count'], 'revision': catalog['revision'],
            'import_id': request.import_id, 'workflow_id': accepted['dispatch']['workflow_id'],
            'activity_identities': identities, 'duplicate': 'canonical',
            'unsafe_archive': 'failed_without_publication',
            'telegram_calls': False, 'in_process_activity_worker': False,
        }, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    asyncio.run(run())
