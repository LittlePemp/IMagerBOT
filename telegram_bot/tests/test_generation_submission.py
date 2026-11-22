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
        self.actor = SimpleNamespace(isbanned=False, is_admin=False)
        self.group = SimpleNamespace(active=True)
        self.repositories = SimpleNamespace(
            admission_repository=self.admissions,
            user_repository=SimpleNamespace(get_user_by_id=AsyncMock(return_value=self.actor)),
            image_group_repository=SimpleNamespace(get_group_by_name=AsyncMock(return_value=self.group)),
            image_build_params_repository=SimpleNamespace(get_common_params=AsyncMock()),
        )

        async def prepare(request, *, job_id, client_context):
            envelope = {
                'job_id': job_id, 'source': request.source,
                'parameters': request.model_dump(exclude={'source', 'watermark_text'}),
                'catalog_manifest': submission('template')['request']['catalog_manifest'],
                'seed': 123, 'client_id': 'telegram', 'client_context': client_context,
            }
            if request.watermark_text is not None:
                envelope['watermark_text'] = request.watermark_text
            return make_submission(envelope)

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

    async def test_existing_today_jobs_seed_regular_user_quota(self):
        self.gateway.count_user_jobs_between.return_value = 5
        with self.assertRaises(DailyQuotaExceeded):
            await self.execute()
        self.loader.assert_not_awaited()
        self.assertEqual(await self.admissions.remaining(1), 0)

    async def test_database_administrator_can_accept_more_than_five_images(self):
        self.actor.is_admin = True
        self.gateway.count_user_jobs_between.return_value = 5
        for index in range(8):
            await self.execute(f'admin-{index}')
        self.assertEqual(self.loader.await_count, 8)
        exempt = [row for row in self.collection.documents.values() if row.get('quota_exempt')]
        self.assertEqual(len(exempt), 8)
        self.assertTrue(all(len(row['admissions']) == 1 for row in exempt))

    async def test_permanent_admin_is_unlimited_even_if_database_role_was_changed(self):
        self.actor.isbanned = True
        self.service.fixed_admin_ids = frozenset({1})
        self.gateway.count_user_jobs_between.return_value = 5
        await self.execute('permanent-admin')
        self.loader.assert_awaited_once()
        self.assertTrue(any(row.get('quota_exempt') for row in self.collection.documents.values()))

    async def test_revoked_admin_reverts_to_ordinary_quota_including_prior_acceptances(self):
        self.actor.is_admin = True
        for index in range(5):
            await self.execute(f'admin-{index}')
        self.actor.is_admin = False
        with self.assertRaises(DailyQuotaExceeded):
            await self.execute('after-demotion')
        self.assertEqual(self.loader.await_count, 5)

    async def test_admin_role_is_rechecked_after_transfer_before_acceptance(self):
        self.actor.is_admin = True
        self.gateway.count_user_jobs_between.return_value = 5

        async def load(job_id):
            self.actor.is_admin = False
            return submission(job_id)['request']['source']

        self.loader.side_effect = load
        with self.assertRaises(DailyQuotaExceeded):
            await self.execute()
        self.assertFalse(any(row.get('quota_exempt') for row in self.collection.documents.values()))

    async def test_untrusted_draft_cannot_enable_administrator_quota_exemption(self):
        self.gateway.count_user_jobs_between.return_value = 5
        self.draft.update(is_admin=True, quota_exempt=True)
        with self.assertRaises(DailyQuotaExceeded):
            await self.execute()
        self.loader.assert_not_awaited()

    async def test_administrator_still_obeys_queue_and_ban_policy(self):
        self.actor.is_admin = True
        self.gateway.count_active_jobs.return_value = config.bot.max_pending_jobs
        with self.assertRaisesRegex(ValueError, 'Очередь заполнена'):
            await self.execute()
        self.loader.assert_not_awaited()
        self.gateway.count_active_jobs.return_value = 0
        self.actor.isbanned = True
        with self.assertRaises(AccessDenied):
            await self.execute()

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
    def source(self, *, local=False, download=None):
        from pathlib import Path

        path = str(Path(config.pack_import.bot_api_container_directory) / 'photo.jpg') if local else 'path'
        message = SimpleNamespace(photo=[SimpleNamespace(file_id='file', file_size=None)], bot=SimpleNamespace(
            session=SimpleNamespace(api=SimpleNamespace(is_local=local)),
            get_file=AsyncMock(return_value=SimpleNamespace(file_path=path, file_size=None)),
            download_file=AsyncMock(side_effect=download),
        ))
        return message, SimpleNamespace(upload_source=AsyncMock(return_value={'uploaded': True}))

    def policy(self):
        return replace(config, limits=replace(config.limits, source_file_bytes=3))

    def required_space(self, policy):
        return policy.bot.source_disk_headroom_bytes + policy.limits.source_file_bytes * policy.bot.max_concurrent_uploads

    async def test_unknown_declared_size_cannot_bypass_streaming_limit(self):
        from src.commands.image_generation import upload

        async def download(remote, destination, **kwargs):
            destination.write(b'abcd')

        message, gateway = self.source(download=download)
        policy = self.policy()
        with patch.object(upload, 'config', policy), patch.object(upload.shutil, 'disk_usage',
                return_value=SimpleNamespace(free=self.required_space(policy))):
            with self.assertRaises(ValueError):
                await upload.TelegramPhotoSource(message, gateway)('job-1')
        gateway.upload_source.assert_not_awaited()

    async def test_low_disk_space_rejects_before_requesting_a_telegram_file(self):
        from src.commands.image_generation import upload

        message, gateway = self.source()
        policy = self.policy()
        with patch.object(upload, 'config', policy), patch.object(upload.shutil, 'disk_usage',
                return_value=SimpleNamespace(free=self.required_space(policy) - 1)):
            with self.assertRaisesRegex(ValueError, 'Недостаточно свободного места'):
                await upload.TelegramPhotoSource(message, gateway)('job-1')
        message.bot.get_file.assert_not_awaited()
        message.bot.download_file.assert_not_awaited()
        gateway.upload_source.assert_not_awaited()

    async def test_disk_space_is_rechecked_after_telegram_prepares_the_file(self):
        from src.commands.image_generation import upload

        message, gateway = self.source()
        policy = self.policy()
        required = self.required_space(policy)
        with patch.object(upload, 'config', policy), patch.object(upload.shutil, 'disk_usage', side_effect=[
            SimpleNamespace(free=required), SimpleNamespace(free=required - 1),
        ]) as disk_usage:
            with self.assertRaisesRegex(ValueError, 'Недостаточно свободного места'):
                await upload.TelegramPhotoSource(message, gateway)('job-1')
        message.bot.get_file.assert_awaited_once_with('file')
        self.assertEqual(disk_usage.call_count, 2)
        message.bot.download_file.assert_not_awaited()
        gateway.upload_source.assert_not_awaited()

    async def test_local_api_checks_both_cache_and_temporary_filesystem_before_and_after_get_file(self):
        from collections import Counter
        from pathlib import Path
        import tempfile
        from src.commands.image_generation import upload

        async def download(remote, destination, **kwargs):
            destination.write(b'abc')

        message, gateway = self.source(local=True, download=download)
        policy = self.policy()
        roots = {Path(tempfile.gettempdir()), Path(policy.pack_import.bot_api_container_directory)}
        with patch.object(upload, 'config', policy), patch.object(upload.shutil, 'disk_usage',
                return_value=SimpleNamespace(free=self.required_space(policy))) as disk_usage:
            self.assertEqual(await upload.TelegramPhotoSource(message, gateway)('job-1'), {'uploaded': True})
        self.assertEqual(Counter(call.args[0] for call in disk_usage.call_args_list), Counter({root: 2 for root in roots}))
        message.bot.get_file.assert_awaited_once_with('file')
        message.bot.download_file.assert_awaited_once()
        gateway.upload_source.assert_awaited_once()


class TestPublicGenerationAcknowledgement(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from aiogram import Bot
        from aiogram.types import Chat, Message, User, PhotoSize
        from datetime import datetime, timezone
        from test_bot_routes import FakeTelegramSession
        self.session = FakeTelegramSession()
        self.bot = Bot(token='123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi', session=self.session)
        self.message = Message(
            message_id=34, date=datetime.now(timezone.utc), chat=Chat(id=1, type='private'),
            from_user=User(id=1, first_name='Alice', username='Alice', is_bot=False),
            photo=[PhotoSize(file_id='photo', file_unique_id='photo-unique', width=64, height=64)],
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

    async def test_accepted_and_duplicate_submissions_start_progress_without_a_separate_menu_screen(self):
        from src.commands.image_generation import upload
        private_id = upload.generation_job_id(self.message)
        for duplicate in (False, True):
            with self.subTest(duplicate=duplicate):
                self.state.clear.reset_mock()
                receipt = SimpleNamespace(job_id=private_id, duplicate=duplicate)
                service = SimpleNamespace(execute=AsyncMock(return_value=receipt), gateway=object())
                progress = SimpleNamespace(watch=AsyncMock())
                with patch.object(upload, 'render_view', AsyncMock()) as render:
                    await upload.receive_image(self.message, self.state, self.user, service, progress)
                render.assert_not_awaited()
                progress.watch.assert_awaited_once_with(self.message, job_id=private_id, actor_id=1, duplicate=duplicate)
                if duplicate:
                    self.state.clear.assert_not_awaited()
                else:
                    self.state.clear.assert_awaited_once()
                self.assertEqual(self.session.methods, [])
                context = service.execute.await_args.kwargs['client_context']
                self.assertTrue(context['display_name'].startswith('Alice_'))
                self.assert_public(context['display_name'], private_id)
                self.assertTrue(context['display_name'].endswith('.' + config.render.output_extension))

    async def test_uncertain_acceptance_is_tracked_without_buttons_or_a_second_submission(self):
        from src.commands.image_generation import upload
        from src.infrastructure.data.errors import AdmissionUncertain
        private_id = upload.generation_job_id(self.message)
        service = SimpleNamespace(execute=AsyncMock(side_effect=AdmissionUncertain(private_id)), gateway=object())
        progress = SimpleNamespace(watch=AsyncMock())
        with patch.object(upload, 'render_view', AsyncMock()) as render:
            await upload.receive_image(self.message, self.state, self.user, service, progress)
        render.assert_not_awaited()
        service.execute.assert_awaited_once()
        progress.watch.assert_awaited_once_with(self.message, job_id=private_id, actor_id=1)
        self.state.clear.assert_awaited_once()
        self.assertEqual(self.session.methods, [])

    async def test_raw_identity_in_validation_error_is_not_sent_to_the_user(self):
        from src.commands.image_generation import upload
        private_id = upload.generation_job_id(self.message)
        service = SimpleNamespace(execute=AsyncMock(side_effect=ValueError(f'Задание {private_id} недоступно')), gateway=object())
        progress = SimpleNamespace(watch=AsyncMock())
        await upload.receive_image(self.message, self.state, self.user, service, progress)
        self.assert_public(self.session.last[1].text, private_id)
        progress.watch.assert_not_awaited()
        self.state.clear.assert_not_awaited()
