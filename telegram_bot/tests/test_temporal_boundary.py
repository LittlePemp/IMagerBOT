import asyncio
import copy
import hashlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from src.infrastructure.services.imager_service.imager_service import ImagerService
from src.infrastructure.services.imager_service.notifier import GenerationNotifier
from src.infrastructure.services.imager_service.schemas import GenerateImageRequest
from src.infrastructure.services.object_storage import BotObjectStorage
from src.models.generation_history import generation_history_record_id


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
        if name == 'generation_jobs':
            return self.jobs
        if name == 'catalog_groups':
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
        }
        self.service = SimpleNamespace(
            config=self.config,
            claim_completion=AsyncMock(return_value=self.job),
            renew_completion=AsyncMock(return_value=True),
            acknowledge_completion=AsyncMock(return_value=True),
            fail_completion=AsyncMock(),
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
        self.assertIn('Invalid image', self.bot.send_message.await_args.kwargs['text'])
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
        self.assertEqual(len(collection.documents), 1)
        projection = next(iter(collection.documents.values()))
        self.assertEqual(projection['job_id'], 'job-1')
        self.assertEqual(projection['status'], 'completed')
        self.assertEqual(projection['result_image_path'],
                         's3://imager/jobs/job-1/attempts/token/result.jpg')
        self.bot.send_document.assert_awaited_once()
        self.service.acknowledge_completion.assert_awaited_once()


class TestUploadHandler(unittest.IsolatedAsyncioTestCase):
    async def test_photo_submission_returns_without_waiting_for_render(self):
        from src.commands.image_generation import image_generation_handler as handler

        service = SimpleNamespace(
            get_job=AsyncMock(return_value=None),
            upload_source=AsyncMock(return_value=reference()),
            submit_generation=AsyncMock(),
        )

        async def download(remote, destination):
            Path(destination).write_bytes(b'photo')

        message = SimpleNamespace(
            chat=SimpleNamespace(id=12), message_id=34,
            photo=[SimpleNamespace(file_id='photo')],
            bot=SimpleNamespace(
                get_file=AsyncMock(return_value=SimpleNamespace(file_path='telegram/path')),
                download_file=AsyncMock(side_effect=download),
            ),
            reply=AsyncMock(),
        )
        state = SimpleNamespace(
            get_data=AsyncMock(return_value={
                'image_group': 'animals', 'image_size': 120,
                'noise_level': 15, 'inset_size': 40,
            }),
            clear=AsyncMock(),
        )
        user = SimpleNamespace(
            telegram_id=1, telegram_username='alice', name='Alice', is_admin=True,
        )
        history_id = generation_history_record_id(handler._generation_job_id(message))
        history = SimpleNamespace(create_record=AsyncMock(return_value=history_id))
        with patch.object(handler, 'imager_service', service):
            with patch.object(handler, '_set_processing_reaction', AsyncMock()):
                await handler.receive_image(message, state, SimpleNamespace(generation_history_repository=history), user)
        service.submit_generation.assert_awaited_once()
        state.clear.assert_awaited_once()
        submitted = service.submit_generation.await_args
        self.assertEqual(submitted.args[0].source['bucket'], 'imager')
        self.assertEqual(submitted.kwargs['client_context']['chat_id'], 12)
        self.assertEqual(submitted.kwargs['client_context']['history_record_id'], history_id)
        self.assertTrue(history.create_record.await_args.args[0].source_image_path.startswith('s3://'))
        self.assertIn('Изображение принято', message.reply.await_args.args[0])

        # History is a repairable projection: its outage cannot revoke an
        # already durable inbox command or produce a false rejection reply.
        history.create_record.return_value = None
        with patch.object(handler, 'imager_service', service):
            with patch.object(handler, '_set_processing_reaction', AsyncMock()):
                await handler.receive_image(message, state, SimpleNamespace(
                    generation_history_repository=history), user)
        self.assertIn('Изображение принято', message.reply.await_args.args[0])
        self.assertEqual(state.clear.await_count, 2)

    async def test_concurrent_duplicate_photo_submissions_share_one_inbox_and_history(self):
        from src.commands.image_generation import image_generation_handler as handler

        database = Database()
        service = ImagerService(SimpleNamespace(), database=database)
        service.upload_source = AsyncMock(return_value=reference())
        barrier = asyncio.Event()
        readers = 0

        async def initially_missing(job_id):
            nonlocal readers
            if readers >= 2:
                return database.jobs.find_one({'_id': job_id})
            readers += 1
            if readers == 2:
                barrier.set()
            await barrier.wait()
            return None

        service.get_job = AsyncMock(side_effect=initially_missing)
        async def download(remote, destination):
            Path(destination).write_bytes(b'photo')

        message = SimpleNamespace(
            chat=SimpleNamespace(id=12), message_id=34,
            photo=[SimpleNamespace(file_id='photo')], reply=AsyncMock(),
            bot=SimpleNamespace(
                get_file=AsyncMock(return_value=SimpleNamespace(file_path='telegram/path')),
                download_file=AsyncMock(side_effect=download),
            ),
        )
        state = SimpleNamespace(get_data=AsyncMock(return_value={
            'image_group': 'animals', 'image_size': 120, 'noise_level': 15, 'inset_size': 40,
        }), clear=AsyncMock())
        user = SimpleNamespace(telegram_id=1, telegram_username='alice', is_admin=True)
        collection = HistoryCollection()
        history = history_repository(collection)
        with patch.object(handler, 'imager_service', service):
            with patch.object(handler, '_set_processing_reaction', AsyncMock()):
                await asyncio.gather(*[
                    handler.receive_image(message, state, SimpleNamespace(
                        generation_history_repository=history), user)
                    for _ in range(2)
                ])
        self.assertEqual(len(database.jobs.documents), 1)
        self.assertEqual(len(collection.documents), 1)
        self.assertEqual(state.clear.await_count, 2)
        for call in message.reply.await_args_list:
            self.assertIn('Изображение принято', call.args[0])

    async def test_existing_command_is_acknowledged_without_new_upload_or_history(self):
        from src.commands.image_generation import image_generation_handler as handler

        service = SimpleNamespace(
            get_job=AsyncMock(return_value={'status': 'running'}),
            upload_source=AsyncMock(),
        )
        message = SimpleNamespace(
            chat=SimpleNamespace(id=12), message_id=34, photo=[object()], reply=AsyncMock(),
        )
        state = SimpleNamespace(clear=AsyncMock())
        with patch.object(handler, 'imager_service', service):
            await handler.receive_image(message, state, object(), object())
        service.upload_source.assert_not_awaited()
        self.assertIn('уже принято', message.reply.await_args.args[0])
