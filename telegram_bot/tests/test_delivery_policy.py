import asyncio
import copy
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.methods import SendMessage

from imager_config.loader import config
from src.infrastructure.services.imager_service.delivery_policy import bounded_backoff
from src.infrastructure.services.imager_service.imager_service import ImagerService
from src.infrastructure.services.imager_service.notifier import GenerationNotifier
from src.models.generation_history import generation_history_record_id
from test_temporal_boundary import Database, reference


def job_document():
    return {
        '_id': 'job-1', 'status': 'completed',
        'result': reference('jobs/job-1/result.jpg', b'result'),
        'request': {
            'client_id': 'telegram',
            'client_context': {
                'chat_id': 12, 'user_id': 1,
                'history_record_id': generation_history_record_id('job-1'),
            },
            'source': reference(),
            'parameters': {
                'group_name': 'animals', 'insertion_format': 'crop',
                'alpha_channel': 30, 'noise_level': 15,
                'cell_size': 40, 'result_size': 120,
            },
        },
        'completion_outbox': {'state': 'claimed', 'token': 'send-token', 'attempts': 5},
        'telegram_history_projection': {'state': 'claimed', 'token': 'history-token', 'attempts': 1},
    }


def nested(document, path):
    value = document
    for key in path.split('.'):
        value = value.get(key)
        if value is None:
            return None
    return value


class MutableJob:
    """Small document store exercising the gateway's fenced state updates."""
    def __init__(self, document):
        self.document = copy.deepcopy(document)

    def update_one(self, query, update):
        if any(nested(self.document, key) != value for key, value in query.items()):
            return SimpleNamespace(matched_count=0)
        for operator, values in update.items():
            for path, value in values.items():
                target = self.document
                keys = path.split('.')
                for key in keys[:-1]:
                    target = target.setdefault(key, {})
                if operator == '$set':
                    target[keys[-1]] = value
                elif operator == '$inc':
                    target[keys[-1]] = target.get(keys[-1], 0) + value
                else:
                    raise AssertionError(operator)
        return SimpleNamespace(matched_count=1)


class TestDeliveryFailures(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = MutableJob(job_document())
        self.service = ImagerService(
            SimpleNamespace(bot_notification_max_attempts=5),
            database={config.mongo.jobs_collection: self.store},
        )

    async def test_flood_wait_at_last_attempt_preserves_budget_and_server_delay(self):
        error = TelegramRetryAfter(
            method=SendMessage(chat_id=12, text='test'), message='FloodWait', retry_after=300,
        )
        before = datetime.now(timezone.utc)
        self.assertTrue(await self.service.fail_completion('job-1', 'send-token', 5, error))
        outbox = self.store.document['completion_outbox']
        self.assertEqual(outbox['state'], 'pending')
        self.assertEqual(outbox['attempts'], 4)
        self.assertEqual(outbox['error_kind'], 'rate_limited')
        self.assertGreaterEqual(outbox['available_at'], before + timedelta(seconds=300))

    async def test_permanent_refusal_stops_immediately_without_render_changes(self):
        error = TelegramForbiddenError(
            method=SendMessage(chat_id=12, text='test'), message='bot blocked',
        )
        await self.service.fail_completion('job-1', 'send-token', 1, error)
        self.assertEqual(self.store.document['completion_outbox']['state'], 'failed')
        self.assertEqual(self.store.document['completion_outbox']['error_kind'], 'permanent')
        self.assertEqual(self.store.document['status'], 'completed')
        self.assertEqual(self.store.document['result']['key'], 'jobs/job-1/result.jpg')

    async def test_expired_owner_cannot_change_new_delivery_claim(self):
        self.assertFalse(await self.service.fail_completion('job-1', 'old-token', 1, OSError('offline')))
        self.assertEqual(self.store.document['completion_outbox']['state'], 'claimed')
        self.assertEqual(self.store.document['completion_outbox']['token'], 'send-token')

    async def test_history_outage_has_independent_durable_backoff(self):
        await self.service.fail_history_projection('job-1', 'history-token', 999999, OSError('history down'))
        repair = self.store.document['telegram_history_projection']
        self.assertEqual(repair['state'], 'pending')
        self.assertLessEqual(
            repair['available_at'] - datetime.now(timezone.utc),
            timedelta(seconds=config.bot.history_repair_retry_max_seconds),
        )
        self.assertEqual(self.store.document['completion_outbox']['attempts'], 5)
        self.assertEqual(self.store.document['completion_outbox']['state'], 'claimed')

    def test_long_outage_cannot_overflow_exponential_backoff(self):
        self.assertEqual(bounded_backoff(999999, 2, 2, 60), 60)


class TestIndependentProjection(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.job = job_document()
        self.service = SimpleNamespace(
            config=SimpleNamespace(bot_notification_lease_seconds=120,
                                   telegram_max_document_size_bytes=100,
                                   bot_notification_poll_seconds=0.01),
            claim_completion=AsyncMock(return_value=self.job),
            renew_completion=AsyncMock(return_value=True),
            acknowledge_completion=AsyncMock(return_value=True),
            fail_completion=AsyncMock(),
            claim_history_projection=AsyncMock(return_value=self.job),
            acknowledge_history_projection=AsyncMock(return_value=True),
            fail_history_projection=AsyncMock(),
            object_store=SimpleNamespace(download_result=lambda ref, path, **kw: Path(path).write_bytes(b'result')),
        )
        self.bot = SimpleNamespace(
            send_document=AsyncMock(return_value=SimpleNamespace(message_id=77)),
            send_message=AsyncMock(return_value=SimpleNamespace(message_id=78)),
        )
        self.history = SimpleNamespace(create_record=AsyncMock(return_value=None),
                                       update_record=AsyncMock(return_value=True))
        self.notifier = GenerationNotifier(self.service, self.bot, self.history)

    async def test_broken_history_does_not_prevent_document_or_spend_send_attempts(self):
        await self.notifier.deliver_once()
        self.bot.send_document.assert_awaited_once()
        self.service.acknowledge_completion.assert_awaited_once()
        self.history.create_record.assert_not_awaited()
        await self.notifier.repair_history_once()
        self.service.fail_history_projection.assert_awaited_once()
        self.service.fail_completion.assert_not_awaited()

    async def test_ready_caption_and_legacy_filename_hide_generation_identity(self):
        private_id = 'eac94427-9dc9-5b5b-b55c-2bf4d891eb31'
        self.job['_id'] = private_id
        self.job['created_at'] = datetime(2026, 11, 12, 13, 14, tzinfo=timezone.utc)
        self.job['request']['client_context']['display_name'] = f'imager_{private_id}'
        await self.notifier.deliver_once()
        sent = self.bot.send_document.await_args.kwargs
        self.assertEqual(sent['caption'], 'Ваше изображение готово.')
        self.assertNotIn(private_id, sent['document'].filename)
        self.assertNotIn(private_id.replace('-', ''), sent['document'].filename)
        self.assertIn('20261112T131400', sent['document'].filename)
        self.assertTrue(sent['document'].filename.endswith('.' + config.render.output_extension))
        self.service.acknowledge_completion.assert_awaited_once_with(private_id, 'send-token', 77)

    async def test_frozen_full_filename_is_sent_once_without_double_extension(self):
        filename = 'Alice_20261112T131400000000Z_aabbccddeeff.jpg'
        self.job['request']['client_context']['display_name'] = filename
        await self.notifier.deliver_once()
        sent = self.bot.send_document.await_args.kwargs
        self.assertEqual(sent['document'].filename, filename)
        self.assertEqual(sent['caption'], 'Ваше изображение готово.')

    async def test_failed_generation_message_has_no_identity_or_reference_request(self):
        private_id = 'eac94427-9dc9-5b5b-b55c-2bf4d891eb31'
        self.job.update(_id=private_id, status='failed', error=f'Internal failure for {private_id}')
        await self.notifier.deliver_once()
        sent = self.bot.send_message.await_args.kwargs['text']
        self.assertNotIn(private_id, sent)
        self.assertNotIn('задан', sent.casefold())
        self.assertNotIn('номер', sent.casefold())
        self.bot.send_document.assert_not_awaited()
        self.service.acknowledge_completion.assert_awaited_once_with(private_id, 'send-token', 78)

    async def test_history_can_recover_after_result_was_already_sent(self):
        self.job['completion_outbox']['state'] = 'sent'
        await self.notifier.repair_history_once()
        self.service.fail_history_projection.assert_awaited_once()
        self.history.create_record.return_value = generation_history_record_id('job-1')
        await self.notifier.repair_history_once()
        self.service.acknowledge_history_projection.assert_awaited_once()
        self.bot.send_document.assert_not_awaited()

    async def test_flood_wait_pauses_other_sends_in_same_notifier(self):
        self.bot.send_document.side_effect = TelegramRetryAfter(
            method=SendMessage(chat_id=12, text='test'), message='FloodWait', retry_after=300,
        )
        self.assertTrue(await self.notifier.deliver_once())
        self.assertFalse(await self.notifier.deliver_once())
        self.service.claim_completion.assert_awaited_once()

    async def test_slow_history_does_not_block_delivery_loop(self):
        history_entered, release_history, delivered = (asyncio.Event() for _ in range(3))
        stop = asyncio.Event()

        async def blocked_history(record):
            history_entered.set()
            await release_history.wait()
            return generation_history_record_id('job-1')

        async def acknowledge(*args):
            delivered.set()
            stop.set()
            return True

        self.history.create_record.side_effect = blocked_history
        self.service.acknowledge_completion.side_effect = acknowledge
        task = asyncio.create_task(self.notifier.run(stop))
        try:
            await asyncio.wait_for(history_entered.wait(), 1)
            await asyncio.wait_for(delivered.wait(), 1)
            self.bot.send_document.assert_awaited_once()
        finally:
            stop.set()
            release_history.set()
            await asyncio.wait_for(task, 1)


class TestFrozenSubmission(unittest.IsolatedAsyncioTestCase):
    async def test_retry_uses_original_manifest_policy_and_workflow_prefix(self):
        # Import the implementation module explicitly: package attribute names
        # also expose the process-wide gateway instance.
        import importlib
        module = importlib.import_module('src.infrastructure.services.imager_service.imager_service')
        from src.infrastructure.services.imager_service.schemas import GenerateImageRequest

        database = Database()
        service = ImagerService(SimpleNamespace(), database=database)
        request = GenerateImageRequest(source=reference(), group_name='animals')
        document = await service.prepare_submission(request, job_id='frozen-1', client_context={})
        original = copy.deepcopy(document)
        database.group['manifest'] = reference('catalog/new.json', b'changed manifest')
        changed = replace(config, temporal=replace(
            config.temporal, workflow_id_prefix='changed-',
            render=replace(config.temporal.render, maximum_attempts=99),
        ))
        with patch.object(module, 'runtime_config', changed):
            result = await service.publish_submission(document)
        self.assertEqual(database.jobs.documents['frozen-1'], original)
        self.assertEqual(result.workflow_id, original['dispatch']['workflow_id'])

    async def test_corrupt_snapshot_is_rejected_before_publishing(self):
        from src.infrastructure.services.imager_service.schemas import GenerateImageRequest

        database = Database()
        service = ImagerService(SimpleNamespace(), database=database)
        document = await service.prepare_submission(
            GenerateImageRequest(source=reference(), group_name='animals'),
            job_id='frozen-1', client_context={},
        )
        document['request']['parameters']['noise_level'] += 1
        with self.assertRaisesRegex(ValueError, 'identity'):
            await service.publish_submission(document)
        self.assertEqual(database.jobs.documents, {})
