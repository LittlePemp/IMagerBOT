"""Bot identity supplies branding; accepted requests keep it independently of later edits."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram import Bot
from aiogram.types import User

from src.application.bot_identity import bot_watermark_text
from src.application.generation import SubmitGeneration
from src.infrastructure.services.imager_service.imager_service import ImagerService
from src.infrastructure.services.imager_service.schemas import GenerateImageRequest
from src.utils.middlewares.abuse_control import UploadGate
from test_navigation_security import OfflineSession
import test_generation_submission as submissions
import test_temporal_boundary as boundary


class BotWatermarkIdentityTests(unittest.IsolatedAsyncioTestCase):
    async def test_own_identity_uses_aiogram_get_me_cache_not_message_sender(self):
        bot = Bot(token='123456:OFFLINE_ONLY_TEST_TOKEN_000000000000', session=OfflineSession())
        identity = User(id=bot.id, is_bot=True, first_name='IMager', username='IMagerExampleBot')
        bot.get_me = AsyncMock(return_value=identity)
        try:
            self.assertEqual(bot_watermark_text(await bot.me()), 't.me/IMagerExampleBot')
            self.assertEqual(bot_watermark_text(await bot.me()), 't.me/IMagerExampleBot')
            bot.get_me.assert_awaited_once()
        finally:
            await bot.session.close()

    def test_missing_or_unsafe_identity_and_a_regular_message_sender_are_rejected(self):
        for identity in (SimpleNamespace(is_bot=False, username='HumanUser'),
                         SimpleNamespace(is_bot=True, username=None),
                         SimpleNamespace(is_bot=True, username='wrong/link'),
                         SimpleNamespace(is_bot=True, username='бот'),
                         SimpleNamespace(is_bot=True, username='Bot\nOther'),
                         SimpleNamespace(is_bot=True, username='x' * 129)):
            with self.subTest(identity=identity):
                with self.assertRaises(ValueError):
                    bot_watermark_text(identity)


class WatermarkAdmissionTests(unittest.IsolatedAsyncioTestCase):
    setUp = submissions.TestGenerationSubmission.setUp
    execute = submissions.TestGenerationSubmission.execute

    def configure(self, text):
        self.service = SubmitGeneration(self.repositories, self.gateway, UploadGate(), watermark_text=text)

    async def test_actual_bot_branding_is_frozen_in_new_admission(self):
        self.configure('t.me/OwnBot')
        self.draft['watermark_text'] = 't.me/UntrustedDraft'
        await self.execute()
        request = self.gateway.prepare_submission.await_args.args[0]
        self.assertEqual(request.watermark_text, 't.me/OwnBot')
        stored = await self.admissions.latest_request(1)
        self.assertEqual(stored['watermark_text'], 't.me/OwnBot')
        self.assertNotIn('watermark_text', stored['parameters'])

    async def test_repeated_image_uses_current_identity_but_replayed_command_keeps_original_branding(self):
        self.configure('t.me/OriginalBot')
        first = await self.execute('first')
        self.configure('t.me/RenamedBot')
        saved = await self.service.previous_settings(1)
        self.assertNotIn('watermark_text', saved)
        repeated = await self.service.execute(
            actor_id=1, draft=None, job_id='second', client_context={'chat_id': 1}, source_loader=self.loader,
        )
        self.assertFalse(repeated.duplicate)
        self.assertEqual((await self.admissions.latest_request(1))['watermark_text'], 't.me/RenamedBot')
        self.loader.reset_mock()
        duplicate = await self.execute('first')
        self.assertTrue(duplicate.duplicate)
        self.assertEqual(duplicate.payload_hash, first.payload_hash)
        self.loader.assert_not_awaited()
        first_entry = await self.admissions._find_entry('first')
        self.assertEqual(first_entry[1]['document']['request']['watermark_text'], 't.me/OriginalBot')

    async def test_dispatcher_injects_verified_branding_without_network_calls(self):
        from telegram_bot.main import create_dispatcher

        dispatcher = create_dispatcher(self.repositories, self.gateway, watermark_text='t.me/OwnBot')
        try:
            self.assertEqual(dispatcher['generation_service'].watermark_text, 't.me/OwnBot')
        finally:
            await dispatcher.fsm.close()

    def test_invalid_branding_is_rejected_before_an_image_is_accepted(self):
        with self.assertRaises(ValueError):
            self.configure('bad\nbranding')
        self.loader.assert_not_awaited()
        self.gateway.prepare_submission.assert_not_awaited()


class WatermarkGatewayTests(unittest.IsolatedAsyncioTestCase):
    async def test_gateway_keeps_watermark_outside_calculation_values_and_deduplicates_its_snapshot(self):
        database = boundary.Database()
        service = ImagerService(SimpleNamespace(), database=database)
        request = GenerateImageRequest(source=boundary.reference(), group_name='animals',
                                       watermark_text='t.me/OwnBot')
        original = await service.prepare_submission(request, job_id='branded', client_context={})
        self.assertEqual(original['request']['watermark_text'], 't.me/OwnBot')
        self.assertNotIn('watermark_text', original['request']['parameters'])
        await service.publish_submission(original)
        request.watermark_text = 't.me/RenamedBot'
        self.assertEqual(database.jobs.documents['branded']['request']['watermark_text'], 't.me/OwnBot')
        changed = await service.prepare_submission(request, job_id='branded', client_context={})
        with self.assertRaisesRegex(ValueError, 'другими параметрами'):
            await service.publish_submission(changed)

    def test_request_schema_does_not_coerce_unsafe_watermark_values(self):
        for value in (b't.me/Bot', 123, True, 'bad\ntext'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                GenerateImageRequest(source=boundary.reference(), group_name='animals', watermark_text=value)
