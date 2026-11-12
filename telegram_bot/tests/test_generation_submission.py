import copy
import unittest
import io
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import patch
from dataclasses import replace

from imager_contracts import make_submission
from imager_config.loader import config
from src.application.generation import SubmitGeneration
from src.infrastructure.data.errors import AccessDenied, DailyQuotaExceeded
from src.infrastructure.data.repositories.admission_repository import AdmissionRepository
from src.models.image_build_params import CommonParam, ParamType
from src.utils.middlewares.abuse_control import UploadGate
from test_daily_quota import AtomicCollection, submission


class TestGenerationSubmission(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.collection = AtomicCollection()
        self.admissions = AdmissionRepository(
            self.collection, timezone=config.bot.timezone, daily_limit=5, lease_seconds=60, retry_seconds=3,
        )
        self.actor = SimpleNamespace(isbanned=False, is_admin=True)
        self.group = SimpleNamespace(active=True)
        self.repositories = SimpleNamespace(
            admission_repository=self.admissions,
            user_repository=SimpleNamespace(get_user_by_id=AsyncMock(return_value=self.actor)),
            image_group_repository=SimpleNamespace(get_group_by_name=AsyncMock(return_value=self.group)),
            image_build_params_repository=SimpleNamespace(get_common_params=AsyncMock()),
        )

        async def prepare(request, *, job_id, client_context):
            return make_submission({
                'job_id': job_id, 'source': request.source,
                'parameters': request.model_dump(exclude={'source'}),
                'catalog_manifest': submission('template')['request']['catalog_manifest'],
                'seed': 123, 'client_id': 'telegram', 'client_context': client_context,
            })

        self.gateway = SimpleNamespace(
            get_job=AsyncMock(return_value=None), count_user_jobs_between=AsyncMock(return_value=0),
            count_active_jobs=AsyncMock(return_value=0),
            prepare_submission=AsyncMock(side_effect=prepare), publish_submission=AsyncMock(),
        )
        self.loader = AsyncMock(return_value=submission('source')['request']['source'])
        self.service = SubmitGeneration(self.repositories, self.gateway, UploadGate())
        self.draft = {'image_group': 'animals', 'image_size': 120, 'inset_size': 40, 'noise_level': 15}

    async def execute(self, job_id='job-1', actor_id=1):
        return await self.service.execute(
            actor_id=actor_id, draft=copy.deepcopy(self.draft), job_id=job_id,
            client_context={'chat_id': actor_id, 'generation_mode': 'manual'}, source_loader=self.loader,
        )

    async def test_acceptance_is_durable_before_dispatch_and_does_not_depend_on_history(self):
        receipt = await self.execute()
        self.assertEqual((receipt.actor_id, receipt.state), (1, 'pending'))
        self.gateway.publish_submission.assert_not_awaited()
        found = await self.admissions.find('job-1')
        self.assertEqual(found.payload_hash, receipt.payload_hash)
        self.assertEqual(await self.admissions.remaining(1), 4)

    async def test_duplicate_is_not_uploaded_or_charged_again(self):
        first, again = await self.execute(), await self.execute()
        self.assertEqual(first.job_id, again.job_id)
        self.assertTrue(again.duplicate)
        self.loader.assert_awaited_once()
        self.assertEqual(await self.admissions.remaining(1), 4)

    async def test_receipt_is_not_returned_to_another_actor(self):
        await self.execute()
        with self.assertRaises(AccessDenied):
            await self.execute(actor_id=2)

    async def test_existing_today_jobs_seed_quota_including_administrators(self):
        self.gateway.count_user_jobs_between.return_value = 5
        with self.assertRaises(DailyQuotaExceeded):
            await self.execute()
        self.loader.assert_not_awaited()
        self.assertEqual(await self.admissions.remaining(1), 0)

    async def test_only_first_admission_initializes_legacy_count(self):
        await self.execute('job-1')
        self.gateway.count_user_jobs_between.return_value = 5
        await self.execute('job-2')
        self.gateway.count_user_jobs_between.assert_awaited_once()
        self.assertEqual(await self.admissions.remaining(1), 3)

    async def test_blocked_actor_and_disabled_group_fail_before_download(self):
        self.actor.isbanned = True
        with self.assertRaises(AccessDenied):
            await self.execute()
        self.actor.isbanned = False
        self.group.active = False
        with self.assertRaises(ValueError):
            await self.execute()
        self.loader.assert_not_awaited()

    async def test_group_policy_is_rechecked_after_slow_transfer(self):
        async def load(job_id):
            self.group.active = False
            return submission(job_id)['request']['source']
        self.loader.side_effect = load
        with self.assertRaises(ValueError):
            await self.execute()
        self.assertEqual(await self.admissions.remaining(1), 5)
        self.gateway.prepare_submission.assert_not_awaited()

    async def test_deleted_preset_does_not_become_an_unchecked_value(self):
        self.draft['selected_presets'] = {'image_size': {'name': 'old', 'value': 120}}
        self.repositories.image_build_params_repository.get_common_params.return_value = SimpleNamespace(is_success=True, value=[])
        with self.assertRaises(ValueError):
            await self.execute()
        self.loader.assert_not_awaited()

    async def test_full_global_backlog_rejects_before_transfer(self):
        self.gateway.count_active_jobs.return_value = config.bot.max_pending_jobs
        with self.assertRaisesRegex(ValueError, 'Очередь заполнена'):
            await self.execute()
        self.loader.assert_not_awaited()

    async def test_transfer_failure_does_not_charge_quota(self):
        self.loader.side_effect = OSError('transfer failed')
        with self.assertRaises(OSError):
            await self.execute()
        self.assertEqual(await self.admissions.remaining(1), 5)

    async def test_invalid_dimensions_are_rejected_before_profile_lookup_or_transfer(self):
        for invalid in (0, -1, True, 1.5, config.limits.cell_size + 1):
            with self.subTest(cell_size=invalid):
                self.draft['inset_size'] = invalid
                with self.assertRaises(ValueError):
                    await self.execute()
        self.repositories.user_repository.get_user_by_id.assert_not_awaited()
        self.loader.assert_not_awaited()


class TestBoundedTelegramSource(unittest.IsolatedAsyncioTestCase):
    async def test_unknown_declared_size_cannot_bypass_streaming_limit(self):
        from src.commands.image_generation import upload
        async def download(remote, destination, **kwargs):
            destination.write(b'abcd')
        message = SimpleNamespace(photo=[SimpleNamespace(file_id='file')], bot=SimpleNamespace(
            get_file=AsyncMock(return_value=SimpleNamespace(file_path='path', file_size=None)),
            download_file=AsyncMock(side_effect=download),
        ))
        gateway = SimpleNamespace(upload_source=AsyncMock())
        policy = replace(config, limits=replace(config.limits, source_file_bytes=3))
        with patch.object(upload, 'config', policy):
            with self.assertRaises(ValueError):
                await upload.TelegramPhotoSource(message, gateway)('job-1')
        gateway.upload_source.assert_not_awaited()


class TestPublicGenerationAcknowledgement(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from aiogram import Bot
        from aiogram.types import Chat, Message, User
        from datetime import datetime, timezone
        from test_bot_routes import FakeTelegramSession
        self.session = FakeTelegramSession()
        self.bot = Bot(token='123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi', session=self.session)
        self.message = Message(
            message_id=34, date=datetime.now(timezone.utc), chat=Chat(id=1, type='private'),
            from_user=User(id=1, first_name='Alice', username='Alice', is_bot=False),
        ).as_(self.bot)
        self.state = SimpleNamespace(
            get_data=AsyncMock(return_value={'generation_mode': 'manual'}),
            clear=AsyncMock(), update_data=AsyncMock(),
        )
        self.user = SimpleNamespace(telegram_id=1, telegram_username='Alice')

    async def asyncTearDown(self):
        await self.bot.session.close()
        self.assertEqual(self.session.unsupported, [])

    def assert_public(self, text, private_id):
        self.assertNotIn(private_id, text)
        self.assertNotIn(private_id.replace('-', ''), text)
        self.assertNotIn('задан', text.casefold())

    async def test_accepted_and_duplicate_messages_keep_identity_only_in_server_actions(self):
        from src.commands.image_generation import upload
        private_id = upload.generation_job_id(self.message)
        for duplicate in (False, True):
            with self.subTest(duplicate=duplicate):
                receipt = SimpleNamespace(job_id=private_id, duplicate=duplicate)
                service = SimpleNamespace(execute=AsyncMock(return_value=receipt), gateway=object())
                await upload.receive_image(self.message, self.state, self.user, service)
                rendered = self.session.last[1]
                self.assert_public(rendered.text, private_id)
                buttons = [button for row in rendered.reply_markup.inline_keyboard for button in row]
                self.assertIn('Статус изображения', [button.text for button in buttons])
                for button in buttons:
                    self.assert_public(button.text, private_id)
                    self.assertNotIn(private_id, button.callback_data)
                records = self.state.update_data.await_args.kwargs['_current_view']['records']
                self.assertIn(f'job_status:{private_id}', records.values())
                context = service.execute.await_args.kwargs['client_context']
                self.assertTrue(context['display_name'].startswith('Alice_'))
                self.assert_public(context['display_name'], private_id)
                self.assertTrue(context['display_name'].endswith('.' + config.render.output_extension))

    async def test_uncertain_acceptance_is_not_claimed_as_success_and_keeps_exact_status_action(self):
        from src.commands.image_generation import upload
        from src.infrastructure.data.errors import AdmissionUncertain
        private_id = upload.generation_job_id(self.message)
        service = SimpleNamespace(execute=AsyncMock(side_effect=AdmissionUncertain(private_id)), gateway=object())
        await upload.receive_image(self.message, self.state, self.user, service)
        rendered = self.session.last[1]
        self.assert_public(rendered.text, private_id)
        self.assertIn('не удалось подтвердить', rendered.text)
        self.assertNotIn('Изображение принято', rendered.text)
        buttons = [button for row in rendered.reply_markup.inline_keyboard for button in row]
        self.assertIn('Проверить статус', [button.text for button in buttons])
        for button in buttons:
            self.assertNotIn(private_id, button.callback_data)
        records = self.state.update_data.await_args.kwargs['_current_view']['records']
        self.assertIn(f'job_status:{private_id}', records.values())
        self.state.clear.assert_not_awaited()

    async def test_raw_identity_in_validation_error_is_not_sent_to_the_user(self):
        from src.commands.image_generation import upload
        private_id = upload.generation_job_id(self.message)
        service = SimpleNamespace(execute=AsyncMock(side_effect=ValueError(f'Задание {private_id} недоступно')), gateway=object())
        await upload.receive_image(self.message, self.state, self.user, service)
        self.assert_public(self.session.last[1].text, private_id)
