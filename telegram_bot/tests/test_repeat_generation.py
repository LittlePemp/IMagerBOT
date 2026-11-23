"""A new image can reuse durable settings without retaining a wizard or source."""
import copy
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiogram.types import Chat, Message, PhotoSize, Sticker, Update
from bson.int64 import Int64

from imager_config.loader import config
from src.application.generation import GenerationSettingsUnavailable
from src.commands.image_generation.image_generation_states import ImageGenerationStates as S
from src.commands.image_generation.upload import generation_job_id
from src.infrastructure.data.errors import AccessDenied, DailyQuotaExceeded
from test_daily_quota import submission
import test_bot_routes as routes
import test_generation_submission as submissions


class RepeatSubmissionTests(unittest.IsolatedAsyncioTestCase):
    setUp = submissions.TestGenerationSubmission.setUp
    execute = submissions.TestGenerationSubmission.execute

    async def repeat(self, job_id='repeat-1', actor_id=1):
        return await self.service.execute(
            actor_id=actor_id, draft=None, job_id=job_id,
            client_context={'chat_id': actor_id, 'generation_mode': 'repeat'}, source_loader=self.loader,
        )

    async def test_saved_settings_survive_a_new_service_and_exclude_source_identity_and_presets(self):
        await self.execute()
        self.draft.update(image_size=32, inset_size=8)
        saved = await self.service.previous_settings(1)
        self.assertEqual(saved, {
            'image_group': 'animals', 'image_size': 120, 'inset_size': 40, 'noise_level': 15,
            'insertion_format': config.generation.insertion_format,
            'alpha_channel': config.generation.alpha_channel,
        })
        from src.application.generation import SubmitGeneration
        from src.utils.middlewares.abuse_control import UploadGate
        self.service = SubmitGeneration(self.repositories, self.gateway, UploadGate())
        self.loader.reset_mock()
        source = copy.deepcopy(submission('new-image')['request']['source'])
        self.loader.return_value = source
        receipt = await self.repeat()
        self.loader.assert_awaited_once_with('repeat-1')
        request = self.gateway.prepare_submission.await_args.args[0]
        self.assertEqual(request.source, source)
        self.assertEqual((request.result_size, request.cell_size, request.group_name), (120, 40, 'animals'))
        self.assertEqual(receipt.job_id, 'repeat-1')
        self.assertFalse(receipt.duplicate)
        self.repositories.image_build_params_repository.get_common_params.assert_not_awaited()

    async def test_new_image_uses_latest_values_but_duplicate_receipt_is_resolved_before_latest_lookup(self):
        first = await self.execute('first')
        self.draft.update(image_size=64, inset_size=32, noise_level=30)
        await self.execute('second')
        self.assertEqual((await self.service.previous_settings(1))['image_size'], 64)
        self.service.previous_settings = AsyncMock(side_effect=AssertionError('Duplicate must not load settings'))
        self.loader.reset_mock()
        duplicate = await self.repeat('first')
        self.assertEqual(duplicate.payload_hash, first.payload_hash)
        self.assertTrue(duplicate.duplicate)
        self.service.previous_settings.assert_not_awaited()
        self.loader.assert_not_awaited()

    async def test_no_history_or_another_actor_cannot_supply_reusable_settings(self):
        self.assertIsNone(await self.service.previous_settings(1))
        with self.assertRaises(GenerationSettingsUnavailable):
            await self.repeat()
        await self.execute()
        self.assertIsNone(await self.service.previous_settings(2))
        self.loader.reset_mock()
        with self.assertRaises(GenerationSettingsUnavailable):
            await self.repeat(actor_id=2)
        self.loader.assert_not_awaited()

    async def test_repeat_rechecks_current_library_ban_quota_and_backlog_before_transfer(self):
        await self.execute()
        self.loader.reset_mock()
        self.group.active = False
        with self.assertRaises(GenerationSettingsUnavailable):
            await self.repeat()
        self.group.active = True
        self.actor.isbanned = True
        with self.assertRaises(AccessDenied):
            await self.repeat()
        self.actor.isbanned = False
        self.gateway.count_active_jobs.return_value = config.bot.max_pending_jobs
        with self.assertRaisesRegex(ValueError, 'Очередь заполнена'):
            await self.repeat()
        self.gateway.count_active_jobs.return_value = 0
        for index in range(config.bot.regular_user_upload_limit - 1):
            await self.repeat(f'quota-{index}')
        self.loader.reset_mock()
        with self.assertRaises(DailyQuotaExceeded):
            await self.repeat('too-many')
        self.loader.assert_not_awaited()

    async def test_bson_telegram_identity_and_latest_administrator_bucket_work(self):
        identifier = Int64(6000000000)
        self.actor.is_admin = True
        # The route has a ten-second upload cooldown. Model distinct accepted
        # times explicitly rather than depending on the platform clock tick.
        with patch('src.application.generation.datetime', wraps=datetime) as clock:
            clock.now.return_value = datetime.now(timezone.utc) - timedelta(seconds=10)
            await self.execute('older', actor_id=identifier)
        self.draft.update(image_size=128, noise_level=30)
        await self.execute('newer', actor_id=identifier)
        saved = await self.service.previous_settings(int(identifier))
        self.assertEqual((saved['image_size'], saved['noise_level']), (128, 30))
        await self.repeat('large-user-repeat', actor_id=int(identifier))

    async def test_corrupted_or_foreign_frozen_request_cannot_be_reused(self):
        await self.execute()
        bucket = next(row for row in self.collection.documents.values() if row.get('admissions'))
        bucket['admissions'][-1]['document']['request']['client_context']['user_id'] = 2
        self.loader.reset_mock()
        with self.assertRaises(GenerationSettingsUnavailable):
            await self.repeat()
        self.loader.assert_not_awaited()


class RepeatRouteTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = routes.BotRouteTests.asyncSetUp
    asyncTearDown = routes.BotRouteTests.asyncTearDown
    actor = routes.BotRouteTests.actor
    state = routes.BotRouteTests.state

    async def media(self, *, sticker=False, advance=None):
        self.sequence += 1
        self.clock += config.bot.upload_cooldown_seconds + 1 if advance is None else advance
        values = {'sticker': Sticker(
            file_id='new-sticker', file_unique_id='new-sticker-unique', type='regular',
            width=512, height=512, is_animated=False, is_video=False,
        )} if sticker else {'photo': [PhotoSize(
            file_id='new-photo', file_unique_id='new-photo-unique', width=512, height=512,
        )]}
        message = Message(
            message_id=self.sequence, date=datetime.now(timezone.utc),
            chat=Chat(id=3, type='private'), from_user=self.actor(3), **values,
        ).as_(self.bot)
        await self.dispatcher.feed_update(self.bot, Update(update_id=self.sequence, message=message))
        return message

    def submission_service(self, *, error=None):
        self.accept = AsyncMock(side_effect=error)
        self.accept.return_value = SimpleNamespace(job_id='accepted', duplicate=False)
        self.watch = AsyncMock()
        self.dispatcher['generation_service'] = SimpleNamespace(execute=self.accept, gateway=object())
        self.dispatcher['generation_progress'] = SimpleNamespace(watch=self.watch)

    async def test_idle_photo_and_sticker_repeat_without_fsm_settings_even_after_state_loss(self):
        for sticker in (False, True):
            with self.subTest(sticker=sticker):
                await self.state(3).clear()
                self.submission_service()
                message = await self.media(sticker=sticker)
                self.accept.assert_awaited_once()
                arguments = self.accept.await_args.kwargs
                self.assertIsNone(arguments['draft'])
                self.assertEqual(arguments['actor_id'], 3)
                self.assertEqual(arguments['job_id'], generation_job_id(message))
                self.assertEqual(arguments['client_context']['generation_mode'], 'repeat')
                self.watch.assert_awaited_once()
                self.assertIsNone(await self.state(3).get_state())

    async def test_explicit_new_upload_form_has_priority_over_last_settings(self):
        await self.state(3).set_state(S.waiting_for_image_upload)
        await self.state(3).set_data({
            'image_group': 'new-group', 'image_size': 32, 'inset_size': 32, 'noise_level': 5,
            '_current_view': {'expires_at': datetime.now().timestamp() + 60},
        })
        self.submission_service()
        await self.media()
        self.assertEqual(self.accept.await_args.kwargs['draft']['image_group'], 'new-group')

    async def test_replayed_accepted_image_does_not_discard_an_already_open_new_form(self):
        await self.state(3).set_state(S.waiting_for_image_upload)
        draft = {
            'draft_id': 'new-form', 'image_group': 'new-group',
            'image_size': 32, 'inset_size': 32, 'noise_level': 5,
            '_current_view': {'records': {'0': 'back'}},
        }
        await self.state(3).set_data(draft)
        self.submission_service()
        self.accept.return_value = SimpleNamespace(job_id='previously-accepted', duplicate=True)
        await self.media()
        self.assertEqual(await self.state(3).get_state(), S.waiting_for_image_upload.state)
        self.assertEqual(await self.state(3).get_data(), draft)
        self.assertTrue(self.watch.await_args.kwargs['duplicate'])

    async def test_other_active_forms_do_not_accidentally_repeat_a_generation(self):
        await self.state(3).set_state(S.waiting_manual_image_size)
        await self.state(3).set_data({'_current_view': {'expires_at': datetime.now().timestamp() + 60}})
        self.submission_service()
        await self.media()
        self.accept.assert_not_awaited()
        self.watch.assert_not_awaited()
        self.assertEqual(await self.state(3).get_state(), S.waiting_manual_image_size.state)

    async def test_missing_or_disabled_previous_settings_open_setup_without_a_repeating_error(self):
        self.submission_service(error=GenerationSettingsUnavailable('Библиотека больше недоступна.'))
        await self.media()
        self.assertEqual(await self.state(3).get_state(), S.choosing_generation_mode.state)
        text = self.session.last[3].text
        self.assertIn('Библиотека больше недоступна', text)
        self.assertIn('Выберите настройки', text)
        labels = [button.text for row in self.session.last[3].reply_markup.inline_keyboard for button in row]
        self.assertIn('Качество (реком.)', labels)
        self.watch.assert_not_awaited()

    async def test_invalid_settings_in_an_active_upload_form_also_return_to_setup(self):
        for reason in ('Библиотека больше недоступна.', 'Выбранный пресет изменён или удалён.'):
            with self.subTest(reason=reason):
                await self.state(3).set_state(S.waiting_for_image_upload)
                await self.state(3).set_data({
                    'draft_id': 'old-settings', 'image_group': 'old-group',
                    'image_size': 64, 'inset_size': 40, 'noise_level': 30,
                    '_current_view': {'records': {}},
                })
                self.submission_service(error=GenerationSettingsUnavailable(reason))
                await self.media()
                self.assertEqual(await self.state(3).get_state(), S.choosing_generation_mode.state)
                self.assertIn(reason, self.session.last[3].text)
                self.assertNotIn('draft_id', await self.state(3).get_data())
                self.watch.assert_not_awaited()
