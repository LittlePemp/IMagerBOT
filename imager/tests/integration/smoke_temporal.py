"""Explicit live smoke test against the configured, dedicated deployment.

Run from /app/imager (or a core checkout with its normal PYTHONPATH):
    python tests/integration/smoke_temporal.py --jobs 2 --result-size 100

This is deliberately not a pytest test. It imports only generated images and
leaves uniquely named smoke jobs/catalogs in Mongo, MinIO and Temporal for UI
inspection. It does not start services or read/modify a user's image catalog.
"""
import argparse
import asyncio
import copy
import hashlib
import json
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


class SmokeFailure(RuntimeError):
    """An assertion message containing only synthetic identifiers/state."""


def require(condition, message):
    if not condition:
        raise SmokeFailure(message)


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--jobs', type=int, default=1, help='Fan out 1..20 independent jobs.')
    parser.add_argument('--result-size', type=int, default=100,
                        help='Mosaic cells per side, 1..500; every cell is 8x8 pixels.')
    parser.add_argument('--timeout', type=float, default=120,
                        help='Overall live-test deadline in seconds (default: 120).')
    args = parser.parse_args()
    if not 1 <= args.jobs <= 20:
        parser.error('--jobs must be between 1 and 20')
    if not 1 <= args.result_size <= 500:
        parser.error('--result-size must be between 1 and 500')
    if not 0 < args.timeout <= 3600:
        parser.error('--timeout must be between 0 and 3600 seconds')
    return args


async def run_smoke(args):
    # Keep --help usable without settings/dependencies. Configure OpenCV's
    # decoder ceilings before its first import, exactly as the worker does.
    from src.shared_kernel.resource_limits import configure_decoder_limits
    configure_decoder_limits()
    import cv2
    import numpy as np
    from temporalio.client import Client
    from imager_contracts import GenerationParameters, JobRequest, ObjectRef
    from settings import settings
    from src.infrastructure.jobs.mongo_jobs import JobConflict
    from src.infrastructure.object_storage.catalog import import_local_catalog
    from src.infrastructure.runtime_bootstrap import create_job_repository, create_object_store

    started = time.monotonic()
    run_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S') + '_' + uuid4().hex[:10]
    group_name = f'smoke_{run_id}'
    repository = None
    async with asyncio.timeout(args.timeout):
        repository = await asyncio.to_thread(create_job_repository)
        store = await asyncio.to_thread(create_object_store)
        database = repository.collection.database
        client = await Client.connect(settings.temporal_address, namespace=settings.temporal_namespace)

        with tempfile.TemporaryDirectory(prefix='imager-smoke-') as temporary:
            directory = Path(temporary)
            catalog_path = directory / 'catalog'
            catalog_path.mkdir()
            for name, color in (('red.png', (20, 30, 230)), ('blue.png', (220, 40, 30))):
                pixels = np.full((20, 20, 3), color, dtype=np.uint8)
                require(cv2.imwrite(str(catalog_path / name), pixels), 'Synthetic catalog encoder failed')

            imported = await asyncio.to_thread(
                import_local_catalog, catalog_path, store, database.catalog_groups,
                group_name=group_name,
            )
            require(len(imported) == 1 and imported[0]['count'] == 2,
                    'Synthetic catalog did not publish exactly two images')
            manifest = ObjectRef.from_dict(imported[0]['manifest'])
            x = np.tile(np.arange(64, dtype=np.uint8) * 4, (64, 1))
            source_pixels = np.dstack((x, np.flipud(x.T), np.fliplr(x)))
            source_path = directory / 'source.jpg'
            require(cv2.imwrite(str(source_path), source_pixels), 'Synthetic source encoder failed')
            requests = []
            for index in range(args.jobs):
                job_id = f'smoke_{run_id}_{index + 1:03d}'
                source = await asyncio.to_thread(
                    store.upload_file, source_path, f'uploads/{job_id}/source.jpg', 'image/jpeg',
                )
                request = JobRequest(
                    job_id=job_id, source=ObjectRef.from_dict(source),
                    parameters=GenerationParameters(
                        group_name=group_name, cell_size=8, result_size=args.result_size,
                        insertion_format='crop', alpha_channel=30, noise_level=0,
                    ),
                    catalog_manifest=manifest, seed=index + 1, client_id='smoke',
                    client_context={'run_id': run_id, 'index': index + 1, 'synthetic': True},
                ).to_dict()
                await asyncio.to_thread(repository.receive, request)
                requests.append(request)

            # Submission goes through the actual Mongo inbox -> dispatcher ->
            # Temporal -> worker path. The test never starts a workflow itself.
            job_ids = [request['job_id'] for request in requests]
            while True:
                documents = await asyncio.to_thread(
                    lambda: list(repository.collection.find({'_id': {'$in': job_ids}})),
                )
                by_id = {document['_id']: document for document in documents}
                failed = [job_id for job_id, doc in by_id.items() if doc['status'] == 'failed']
                require(not failed, f'Synthetic jobs failed: {", ".join(failed)}; inspect worker/UI logs')
                if len(by_id) == args.jobs and all(
                    doc['status'] == 'completed' and doc['dispatch']['state'] == 'sent'
                    for doc in by_id.values()
                ):
                    break
                await asyncio.sleep(0.5)

            summaries = []
            for request in requests:
                job_id = request['job_id']
                document = by_id[job_id]
                workflow_id = f'imager:{job_id}'
                handle = client.get_workflow_handle(workflow_id)
                workflow_result = await handle.result()
                reference = ObjectRef.from_dict(document['result']).to_dict()
                require(workflow_result == reference, f'{job_id}: Temporal/Mongo result mismatch')
                description = await handle.describe()
                require(description.status is not None and description.status.name == 'COMPLETED',
                        f'{job_id}: Temporal did not complete successfully')
                outbox = document.get('completion_outbox') or {}
                require(outbox.get('event_id') == f'{job_id}:completed'
                        and outbox.get('state') == 'pending', f'{job_id}: completion outbox is invalid')
                execution = document['execution']
                require(execution['fence'] == 1, f'{job_id}: expected one initial execution claim')
                require(reference['key'] == f'jobs/{job_id}/attempts/{execution["token"]}/result.jpg',
                        f'{job_id}: published result is outside its execution attempt')

                result_path = directory / f'{job_id}.jpg'
                await asyncio.to_thread(store.download_file, reference, result_path)
                encoded = result_path.read_bytes()
                require(hashlib.sha256(encoded).hexdigest() == reference['sha256']
                        and len(encoded) == reference['size_bytes'], f'{job_id}: result checksum mismatch')
                decoded = cv2.imread(str(result_path), cv2.IMREAD_COLOR)
                side = args.result_size * 8
                require(decoded is not None and decoded.shape == (side, side, 3),
                        f'{job_id}: result shape differs from {side}x{side}x3')

                # A completed inbox replay and execution replay return the same
                # canonical result; neither may reset state or increment fence.
                duplicate = await asyncio.to_thread(repository.receive, copy.deepcopy(request))
                replay = await asyncio.to_thread(repository.claim_execution, request)
                require(duplicate['status'] == 'completed' and replay['already_completed']
                        and replay['result'] == reference, f'{job_id}: duplicate caused another execution')
                conflicting = copy.deepcopy(request)
                conflicting['seed'] += 1000
                try:
                    await asyncio.to_thread(repository.receive, conflicting)
                except JobConflict:
                    pass
                else:
                    raise SmokeFailure(f'{job_id}: changed payload was accepted for the same job ID')
                after = await asyncio.to_thread(repository.collection.find_one, {'_id': job_id})
                count = await asyncio.to_thread(repository.collection.count_documents, {'_id': job_id})
                require(count == 1 and after['execution']['fence'] == execution['fence']
                        and after['result'] == reference and after['payload_hash'] == document['payload_hash'],
                        f'{job_id}: duplicate/conflict changed the accepted job')
                history = await handle.fetch_history()
                identities = sorted({event.activity_task_started_event_attributes.identity
                                     for event in history.events
                                     if event.HasField('activity_task_started_event_attributes')})
                summaries.append({
                    'job_id': job_id, 'workflow_id': workflow_id, 'status': 'completed',
                    'fence': execution['fence'], 'shape': [side, side, 3],
                    'size_bytes': reference['size_bytes'], 'result_key': reference['key'],
                    'worker_identities': identities,
                })

            return {'ok': True, 'run_id': run_id, 'group': group_name,
                    'jobs': summaries, 'elapsed_seconds': round(time.monotonic() - started, 2),
                    'duplicate_reused_result': True, 'payload_conflict_rejected': True,
                    'completion_outbox': 'pending',
                    'examples_retained': True}


def main():
    args = parse_args()
    try:
        report = asyncio.run(run_smoke(args))
    except TimeoutError:
        report = {'ok': False, 'error_type': 'TimeoutError',
                  'message': f'Smoke run exceeded {args.timeout:g} seconds; inspect smoke jobs in the UI'}
    except Exception as error:
        # Do not echo arbitrary infrastructure exception strings: they can
        # contain connection URIs. Service logs retain the underlying failure.
        report = {'ok': False, 'error_type': type(error).__name__,
                  'message': str(error) if isinstance(error, SmokeFailure)
                  else 'Live smoke infrastructure failed; inspect service logs'}
    print(json.dumps(report, ensure_ascii=False, sort_keys=True), flush=True)
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
