import copy
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram.filters import CommandObject
from pymongo.errors import ConnectionFailure

from imager_config.loader import config
from src.commands import jobs
from src.infrastructure.services.imager_service.imager_service import ImagerService
from src.models.admission import AdmissionReceipt
from src.utils.navigation import CURRENT_VIEW
from test_delivery_policy import MutableJob, job_document, nested
from test_navigation_security import TelegramFixture


class RetryStore(MutableJob):
    def __init__(self, document):
        super().__init__(document)
        self.calls = 0

    def update_one(self, query, update):
        self.calls += 1
        for key, expected in query.items():
            actual = nested(self.document, key)
            if isinstance(expected, dict):
                if expected != {'$ne': None}:
                    raise AssertionError(expected)
                if actual is None:
                    return SimpleNamespace(matched_count=0)
            elif actual != expected:
                return SimpleNamespace(matched_count=0)
        return super().update_one(
            {key: value for key, value in query.items() if not isinstance(value, dict)}, update,
        )


class TestJobRecovery(TelegramFixture):
    def setup_job(self):
        document = job_document()
        document['request']['client_context']['user_id'] = 5
        document['completion_outbox'].update(state='failed', error='private technical details')
        document['dispatch'] = {'state': 'sent', 'workflow_id': 'original-workflow'}
        self.store = RetryStore(document)
        self.gateway = ImagerService(SimpleNamespace(), database={config.mongo.jobs_collection: self.store})
        self.gateway.get_job = AsyncMock(side_effect=lambda job_id: copy.deepcopy(self.store.document))
        self.receipt = AdmissionReceipt('job-1', 'sent', '2026-11-12', 'hash', actor_id=5)
        self.admissions = SimpleNamespace(find=AsyncMock(return_value=self.receipt),
                                          accept_submission=AsyncMock())
        self.generation = SimpleNamespace(gateway=self.gateway, admissions=self.admissions)
        self.user = SimpleNamespace(telegram_id=5)

    async def test_owned_failed_delivery_requeues_without_render_or_quota_change(self):
        self.setup_job()
        original = copy.deepcopy(self.store.document)
        before = datetime.now(timezone.utc)
        self.assertTrue(await self.gateway.retry_delivery('job-1', 5))
        current = self.store.document
        self.assertEqual(current['status'], original['status'])
        self.assertEqual(current['result'], original['result'])
        self.assertEqual(current['dispatch'], original['dispatch'])
        self.assertEqual(current['request'], original['request'])
        self.assertEqual(current['completion_outbox']['state'], 'pending')
        self.assertEqual(current['completion_outbox']['attempts'], 0)
        self.assertIsNone(current['completion_outbox']['error'])
        self.assertIsNone(current['completion_outbox']['token'])
        self.assertGreaterEqual(current['completion_outbox']['available_at'], before)
        self.admissions.accept_submission.assert_not_awaited()

    async def test_other_owner_and_nonfailed_delivery_cannot_be_reset(self):
        self.setup_job()
        self.assertFalse(await self.gateway.retry_delivery('job-1', 6))
        for state in ('pending', 'claimed', 'sent'):
            self.store.document['completion_outbox']['state'] = state
            self.assertFalse(await self.gateway.retry_delivery('job-1', 5))
            self.assertEqual(self.store.document['completion_outbox']['state'], state)
        self.store.document['completion_outbox']['state'] = 'failed'
        self.store.document['result'] = None
        self.assertFalse(await self.gateway.retry_delivery('job-1', 5))

    async def test_invalid_id_never_reaches_database(self):
        self.setup_job()
        with self.assertRaises(ValueError):
            await self.gateway.retry_delivery('../invalid', 5)
        self.assertEqual(self.store.calls, 0)
        await jobs.status_command(self.message(), self.state, self.user, self.generation,
                                  CommandObject(prefix='/', command='status', args='../invalid'))
        self.admissions.find.assert_not_awaited()
        self.gateway.get_job.assert_not_awaited()

    async def test_status_separates_rendering_and_notification_and_offers_retry(self):
        self.setup_job()
        await jobs.show_status(self.message(), self.state, self.user, self.generation, 'job-1')
        response = self.session.requests[-1]
        self.assertIn('Сборка: готово', response.text)
        self.assertIn('Доставка: не доставлено', response.text)
        self.assertNotIn('private technical details', response.text)
        actions = (await self.state.get_data())[CURRENT_VIEW]['records'].values()
        self.assertIn('job_retry:job-1', actions)

    async def test_status_of_another_user_reveals_no_job_details(self):
        self.setup_job()
        self.user.telegram_id = 6
        await jobs.show_status(self.message(), self.state, self.user, self.generation, 'job-1')
        self.assertEqual(self.session.requests[-1].text, jobs.UNKNOWN_JOB_TEXT)
        self.gateway.get_job.assert_not_awaited()

    async def test_pending_receipt_remains_visible_during_core_database_outage(self):
        self.setup_job()
        self.admissions.find.return_value = AdmissionReceipt(
            'job-1', 'pending', '2026-11-12', 'hash', actor_id=5,
        )
        self.gateway.get_job.side_effect = ConnectionFailure('core down')
        await jobs.show_status(self.message(), self.state, self.user, self.generation, 'job-1')
        self.assertIn('ожидает передачи ядру', self.session.requests[-1].text)
        self.assertNotIn('core down', self.session.requests[-1].text)

    async def test_retry_command_uses_existing_result_and_cannot_schedule_twice(self):
        self.setup_job()
        await jobs.retry_delivery(self.message(), self.state, self.user, self.generation, 'job-1')
        self.assertIn('Повторная доставка запланирована', self.session.requests[-1].text)
        self.assertEqual(self.store.document['completion_outbox']['state'], 'pending')
        await jobs.retry_delivery(self.message(), self.state, self.user, self.generation, 'job-1')
        self.assertIn('Повтор сейчас недоступен', self.session.requests[-1].text)
        self.admissions.accept_submission.assert_not_awaited()
