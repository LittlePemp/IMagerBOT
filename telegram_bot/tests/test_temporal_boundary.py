import asyncio
import copy
import hashlib
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from src.infrastructure.services.imager_service.imager_service import ImagerService
from src.infrastructure.services.imager_service.notifier import GenerationNotifier
from src.infrastructure.services.imager_service.schemas import GenerateImageRequest
from src.infrastructure.services.object_storage import BotObjectStorage
from src.models.generation_history import generation_history_record_id
from imager_config.loader import config


class HistoryCollection:
    def __init__(self):
        self.documents = {}

    async def update_one(self, query, update, upsert=False):
        identifier = query['_id']
        if identifier not in self.documents and upsert:
            self.documents[identifier] = copy.deepcopy(update['$setOnInsert'])
        if identifier in self.documents:
            self.documents[identifier].update(copy.deepcopy(update.get('$set', {})))
        return SimpleNamespace(matched_count=int(identifier in self.documents))


def history_repository(collection):
    from src.infrastructure.data.repositories.generation_history_repository import GenerationHistoryRepository

    repository = object.__new__(GenerationHistoryRepository)
    repository.collection = collection
    return repository


def reference(key='uploads/job/source.jpg', data=b'pixels'):
    return {
        'bucket': 'imager', 'key': key, 'sha256': hashlib.sha256(data).hexdigest(),
        'size_bytes': len(data), 'content_type': 'image/jpeg',
    }


class Jobs:
    def __init__(self):
        self.documents = {}

    def update_one(self, query, update, upsert=False):
        identifier = query['_id']
        if identifier not in self.documents and upsert:
            self.documents[identifier] = copy.deepcopy(update['$setOnInsert'])
        return SimpleNamespace(matched_count=int(identifier in self.documents))

    def find_one(self, query):
        return copy.deepcopy(self.documents.get(query['_id']))


class Database:
    def __init__(self):
        self.jobs = Jobs()
        self.group = {'_id': 'animals', 'group': 'animals', 'count': 4,
                      'manifest': reference('catalog/animals/manifest.json', b'manifest')}

    def __getitem__(self, name):
        if name == config.mongo.jobs_collection:
            return self.jobs
        if name == config.mongo.catalog_collection:
            return SimpleNamespace(find_one=lambda query: copy.deepcopy(self.group))
        raise AssertionError(name)


class TestSubmission(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.database = Database()
        self.service = ImagerService(SimpleNamespace(), database=self.database)
        self.request = GenerateImageRequest(
            source=reference(), group_name='animals', noise_level=7,
            cell_size=51, result_size=137,
        )

    async def test_same_job_is_deduplicated_and_parameter_snapshot_is_preserved(self):
        context = {'chat_id': 12, 'history_record_id': 'history'}
        first = await self.service.submit_generation(
            self.request, job_id='job-1', client_context=context
        )
        second = await self.service.submit_generation(
            self.request, job_id='job-1', client_context=context
        )
        self.assertEqual(first.job_id, second.job_id)
        self.assertEqual(len(self.database.jobs.documents), 1)
        stored = self.database.jobs.documents['job-1']
        self.assertEqual(stored['dispatch']['state'], 'pending')
        self.assertEqual(stored['request']['parameters']['noise_level'], 7)
        self.request.noise_level = 25
        self.assertEqual(stored['request']['parameters']['noise_level'], 7)

    async def test_reusing_identifier_with_other_parameters_is_rejected(self):
        await self.service.submit_generation(
            self.request, job_id='job-1', client_context={}
        )
        self.request.noise_level = 99
        with self.assertRaisesRegex(ValueError, 'другими параметрами'):
            await self.service.submit_generation(
                self.request, job_id='job-1', client_context={}
            )

    async def test_missing_catalog_is_not_enqueued(self):
        self.database.group = None
        with self.assertRaises(ValueError):
            await self.service.submit_generation(
                self.request, job_id='job-1', client_context={}
            )
        self.assertEqual(self.database.jobs.documents, {})


class Minio:
    def __init__(self):
        self.objects = {}
        self.parallelism = []

    def bucket_exists(self, bucket):
        return True

    def fput_object(self, bucket, key, path, **kwargs):
        self.objects[(bucket, key)] = Path(path).read_bytes()
        self.parallelism.append(kwargs['num_parallel_uploads'])

    def stat_object(self, bucket, key):
        return SimpleNamespace(size=len(self.objects[(bucket, key)]))

    def fget_object(self, bucket, key, path):
        Path(path).write_bytes(self.objects[(bucket, key)])


class TestObjectStorage(unittest.TestCase):
    def test_nondefault_source_key_and_accepted_legacy_result_key(self):
        from src.infrastructure.services.object_storage import minio_storage

        configured = replace(config, storage=replace(
            config.storage, source_key_template='new-sources/{sha256}/{job_id}.jpg',
        ))
        client = Minio()
        storage = BotObjectStorage('', '', '', 'imager', client=client)
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'photo.jpg'
            source.write_bytes(b'source-photo')
            with patch.object(minio_storage, 'config', configured):
                uploaded = storage.upload_source(source, 'job-1')
                self.assertEqual(uploaded['key'],
                                 f"new-sources/{uploaded['sha256']}/job-1.jpg")
                # Immutable accepted references remain valid after key-layout
                # configuration changes, including an older output prefix.
                old_result = reference('previous-output/job-1/final.jpg', b'finished')
                client.objects[('imager', old_result['key'])] = b'finished'
                output = Path(directory) / 'download.jpg'
                storage.download_result(old_result, output, max_bytes=100)
                self.assertEqual(output.read_bytes(), b'finished')

    def test_upload_and_result_download_verify_hashes(self):
        client = Minio()
        storage = BotObjectStorage('', '', '', 'imager', client=client)
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'source.jpg'
            source.write_bytes(b'source-photo')
            uploaded = storage.upload_source(source, 'job-1')
            self.assertEqual(client.objects[('imager', uploaded['key'])], b'source-photo')
            self.assertEqual(client.parallelism, [1])
            result = reference('jobs/job-1/attempts/token/result.jpg', b'final-result')
            client.objects[('imager', result['key'])] = b'final-result'
            output = Path(directory) / 'result.jpg'
            storage.download_result(result, output, max_bytes=100)
            self.assertEqual(output.read_bytes(), b'final-result')

    def test_invalid_download_hash_removes_transfer_file(self):
        client = Minio()
        storage = BotObjectStorage('', '', '', 'imager', client=client)
        result = reference('jobs/job-1/attempts/token/result.jpg', b'correct')
        client.objects[('imager', result['key'])] = b'invalid'
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'result.jpg'
            with self.assertRaisesRegex(ValueError, 'checksum'):
                storage.download_result(result, output, max_bytes=100)
            self.assertFalse(output.exists())

    def test_size_limit_is_checked_before_download(self):
        client = Minio()
        storage = BotObjectStorage('', '', '', 'imager', client=client)
        result = reference('jobs/job-1/attempts/token/result.jpg', b'large-result')
        with self.assertRaisesRegex(ValueError, 'size limit'):
            storage.download_result(result, 'unused.jpg', max_bytes=1)
        self.assertEqual(client.objects, {})


class TestNotifier(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.config = SimpleNamespace(
            bot_notification_lease_seconds=120, telegram_max_document_size_bytes=100,
        )
        self.job = {
            '_id': 'job-1', 'status': 'completed',
            'result': reference('jobs/job-1/attempts/token/result.jpg', b'result'),
            'request': {
                'client_context': {'chat_id': 12, 'user_id': 1,
                                   'history_record_id': generation_history_record_id('job-1')},
                'source': reference(),
                'parameters': {'group_name': 'animals', 'insertion_format': 'crop',
                               'alpha_channel': 30, 'noise_level': 15,
                               'cell_size': 40, 'result_size': 120},
            },
            'completion_outbox': {'token': 'lease-1', 'attempts': 1},
            'telegram_history_projection': {'token': 'history-lease', 'attempts': 1},
        }
        self.service = SimpleNamespace(
            config=self.config,
            claim_completion=AsyncMock(return_value=self.job),
            renew_completion=AsyncMock(return_value=True),
            acknowledge_completion=AsyncMock(return_value=True),
            fail_completion=AsyncMock(),
            claim_history_projection=AsyncMock(return_value=self.job),
            acknowledge_history_projection=AsyncMock(return_value=True),
            fail_history_projection=AsyncMock(),
            object_store=SimpleNamespace(download_result=self.download),
        )
        self.paths = []
        self.bot = SimpleNamespace(
            send_document=AsyncMock(return_value=SimpleNamespace(message_id=77)),
            send_message=AsyncMock(return_value=SimpleNamespace(message_id=78)),
        )
        self.history = SimpleNamespace(
            create_record=AsyncMock(return_value=generation_history_record_id('job-1')),
            update_record=AsyncMock(return_value=True),
        )
        self.notifier = GenerationNotifier(self.service, self.bot, self.history)

    def download(self, reference, path, **kwargs):
        Path(path).write_bytes(b'result')
        self.paths.append(Path(path))

    async def test_document_is_delivered_then_acknowledged_and_scratch_removed(self):
        self.assertTrue(await self.notifier.deliver_once())
        self.bot.send_document.assert_awaited_once()
        self.service.acknowledge_completion.assert_awaited_once_with('job-1', 'lease-1', 77)
        self.service.fail_completion.assert_not_awaited()
        self.assertEqual(len(self.paths), 1)
        self.assertFalse(self.paths[0].exists())
        await self.notifier.repair_history_once()
        self.assertEqual(
            self.history.update_record.await_args.kwargs['result_image_path'],
            's3://imager/jobs/job-1/attempts/token/result.jpg',
        )

    async def test_send_failure_does_not_acknowledge_the_outbox(self):
        self.bot.send_document.side_effect = RuntimeError('Telegram unavailable')
        await self.notifier.deliver_once()
        self.service.acknowledge_completion.assert_not_awaited()
        self.service.fail_completion.assert_awaited_once()

    async def test_failed_job_produces_text_notification(self):
        self.job.update(status='failed', result=None, error='Invalid image')
        await self.notifier.deliver_once()
        self.bot.send_document.assert_not_awaited()
        self.assertNotIn('Invalid image', self.bot.send_message.await_args.kwargs['text'])
        self.assertNotIn('job-1', self.bot.send_message.await_args.kwargs['text'])
        self.assertNotIn('задани', self.bot.send_message.await_args.kwargs['text'].lower())
        self.service.acknowledge_completion.assert_awaited_once_with('job-1', 'lease-1', 78)

    async def test_lost_delivery_lease_prevents_sending(self):
        async def lose(job_id, token, lost):
            lost.set()
            await asyncio.sleep(10)

        with patch.object(self.notifier, '_renew', side_effect=lose):
            await self.notifier.deliver_once()
        self.bot.send_document.assert_not_awaited()
        self.service.fail_completion.assert_awaited_once()

    async def test_notification_repairs_history_missing_after_accepted_command(self):
        collection = HistoryCollection()
        self.notifier.history_repository = history_repository(collection)
        await self.notifier.deliver_once()
        await self.notifier.repair_history_once()
        self.assertEqual(len(collection.documents), 1)
        projection = next(iter(collection.documents.values()))
        self.assertEqual(projection['job_id'], 'job-1')
        self.assertEqual(projection['status'], 'completed')
        self.assertEqual(projection['result_image_path'],
                         's3://imager/jobs/job-1/attempts/token/result.jpg')
        self.bot.send_document.assert_awaited_once()
        self.service.acknowledge_completion.assert_awaited_once()


class TestUploadHandler(unittest.IsolatedAsyncioTestCase):
    async def test_handler_uses_durable_admission_instead_of_direct_inbox_or_history(self):
        from src.commands.image_generation import upload
        from src.models.admission import AdmissionReceipt
        message = SimpleNamespace(chat=SimpleNamespace(id=12), message_id=34, answer=AsyncMock())
        state = SimpleNamespace(get_data=AsyncMock(return_value={'generation_mode': 'manual'}), clear=AsyncMock())
        receipt = AdmissionReceipt('job-1', 'pending', '2026-11-12', 'hash', actor_id=1)
        service = SimpleNamespace(execute=AsyncMock(return_value=receipt), gateway=object())
        with patch.object(upload, 'render_view', AsyncMock()) as render:
            await upload.receive_image(message, state, SimpleNamespace(telegram_id=1), service)
        context = service.execute.await_args.kwargs['client_context']
        self.assertEqual(context['chat_id'], 12)
        self.assertEqual(context['reply_to_message_id'], 34)
        self.assertIn('Изображение принято', render.await_args.args[2])
        state.clear.assert_awaited_once()

    async def test_daily_quota_rejection_keeps_draft_and_does_not_acknowledge_acceptance(self):
        from src.commands.image_generation import upload
        from src.infrastructure.data.errors import DailyQuotaExceeded
        from datetime import datetime, timezone
        message = SimpleNamespace(chat=SimpleNamespace(id=12), message_id=34, answer=AsyncMock())
        state = SimpleNamespace(get_data=AsyncMock(return_value={}), clear=AsyncMock())
        service = SimpleNamespace(execute=AsyncMock(side_effect=DailyQuotaExceeded(5, datetime.now(timezone.utc))), gateway=object())
        await upload.receive_image(message, state, SimpleNamespace(telegram_id=1), service)
        state.clear.assert_not_awaited()
        self.assertIn('полночь', message.answer.await_args.args[0])
