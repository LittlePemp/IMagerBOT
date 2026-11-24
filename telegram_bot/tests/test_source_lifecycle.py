import asyncio
import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, Mock

from imager_config.loader import config
from src.application.source_lifecycle import SourceLifecycle
from src.infrastructure.data.errors import AdmissionUncertain
from src.infrastructure.services.object_storage import BotObjectStorage
from src.infrastructure.services.storage_janitor import StorageJanitor
from src.infrastructure.services.imager_service.imager_service import ImagerService
import test_generation_submission as submission_fixtures
from test_daily_quota import submission


class SourcePublicationRaces(IsolatedAsyncioTestCase):
    def setUp(self):
        self.fixture = submission_fixtures.TestGenerationSubmission(
            'test_acceptance_is_durable_before_dispatch_and_does_not_depend_on_history'
        )
        self.fixture.setUp()
        self.job_id = 'source-race'
        self.key = f'uploads/{self.job_id}/' + 'a' * 64 + '.jpg'
        self.now = datetime.now(timezone.utc)
        self.old = self.now - timedelta(seconds=config.bot.orphan_source_grace_seconds + 60)
        self.objects = {self.key: self.old}
        self.fixture.gateway.list_sources = AsyncMock(return_value=[(self.key, self.old)])
        self.fixture.gateway.get_job = AsyncMock(return_value=None)
        self.fixture.gateway.source_bucket = 'imager'
        self.fixture.gateway.source_is_referenced = AsyncMock(return_value=False)

        async def delete(job_id, key, *, older_than=None):
            modified = self.objects.get(key)
            if modified is None or (older_than is not None and modified > older_than):
                return False
            del self.objects[key]
            return True

        self.fixture.gateway.delete_source = AsyncMock(side_effect=delete)
        self.janitor = StorageJanitor(
            self.fixture.gateway, self.fixture.admissions, local_api=False,
            source_lifecycle=self.fixture.service.source_lifecycle, clock=lambda: self.now,
        )

    async def test_uploaded_source_is_pinned_until_durable_admission(self):
        uploaded, continue_admission, listed = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def load(job_id):
            self.objects[self.key] = self.now
            uploaded.set()
            await continue_admission.wait()
            return dict(submission(job_id)['request']['source'], key=self.key)

        async def listing():
            listed.set()
            return [(self.key, self.old)]

        self.fixture.loader.side_effect = load
        self.fixture.gateway.list_sources.side_effect = listing
        producer = asyncio.create_task(self.fixture.execute(self.job_id))
        await asyncio.wait_for(uploaded.wait(), 2)
        sweeping = asyncio.create_task(self.janitor.sweep_orphan_sources())
        await asyncio.wait_for(listed.wait(), 2)
        self.assertFalse(sweeping.done())
        self.fixture.gateway.delete_source.assert_not_awaited()
        continue_admission.set()
        receipt, removed = await asyncio.wait_for(asyncio.gather(producer, sweeping), 2)
        self.assertEqual(receipt.job_id, self.job_id)
        self.assertEqual(removed, 0)
        self.assertIsNotNone(await self.fixture.admissions.find(self.job_id))
        self.assertIn(self.key, self.objects)
        self.assertFalse(self.fixture.service.source_lifecycle._entries)

    async def test_replay_waits_for_an_already_started_orphan_deletion(self):
        deleting, continue_delete = asyncio.Event(), asyncio.Event()
        original_delete = self.fixture.gateway.delete_source.side_effect

        async def delete(*args, **kwargs):
            deleting.set()
            await continue_delete.wait()
            return await original_delete(*args, **kwargs)

        async def load(job_id):
            self.objects[self.key] = self.now
            return dict(submission(job_id)['request']['source'], key=self.key)

        self.fixture.gateway.delete_source.side_effect = delete
        self.fixture.loader.side_effect = load
        sweeping = asyncio.create_task(self.janitor.sweep_orphan_sources())
        await asyncio.wait_for(deleting.wait(), 2)
        producer = asyncio.create_task(self.fixture.execute(self.job_id))
        await asyncio.sleep(0)
        self.fixture.loader.assert_not_awaited()
        continue_delete.set()
        removed, receipt = await asyncio.wait_for(asyncio.gather(sweeping, producer), 2)
        self.assertEqual(removed, 1)
        self.assertEqual(receipt.job_id, self.job_id)
        self.assertIn(self.key, self.objects)

    async def test_stale_listing_keeps_new_source_after_uncertain_admission(self):
        async def load(job_id):
            self.objects[self.key] = self.now
            return dict(submission(job_id)['request']['source'], key=self.key)

        self.fixture.loader.side_effect = load
        self.fixture.admissions.accept_submission = AsyncMock(side_effect=AdmissionUncertain(self.job_id))
        with self.assertRaises(AdmissionUncertain):
            await self.fixture.execute(self.job_id)
        self.assertEqual(await self.janitor.sweep_orphan_sources(), 0)
        self.assertIn(self.key, self.objects)

    async def test_cancelled_waiter_does_not_leak_a_source_guard(self):
        guard = SourceLifecycle()
        entered = asyncio.Event()

        async def waiting():
            entered.set()
            async with guard.protect(self.job_id):
                self.fail('Cancelled waiter acquired the source')

        async with guard.protect(self.job_id):
            task = asyncio.create_task(waiting())
            await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual(guard._entries[self.job_id].users, 1)
        self.assertFalse(guard._entries)

    async def test_cancelled_janitor_drains_its_delete_before_unlocking(self):
        deleting, continue_delete = threading.Event(), threading.Event()

        def remove(bucket, key):
            deleting.set()
            if not continue_delete.wait(5):
                raise TimeoutError('Test did not release its DELETE')
            self.objects.pop(key, None)

        client = SimpleNamespace(
            stat_object=lambda bucket, key: SimpleNamespace(last_modified=self.objects[key]),
            remove_object=remove,
        )
        store = BotObjectStorage(None, None, None, 'imager', client=client)
        gateway = ImagerService(SimpleNamespace(), object_store=store)
        self.fixture.gateway.delete_source = gateway.delete_source

        async def load(job_id):
            self.objects[self.key] = self.now
            return dict(submission(job_id)['request']['source'], key=self.key)

        self.fixture.loader.side_effect = load
        sweeping = asyncio.create_task(self.janitor.sweep_orphan_sources())
        producer = None
        try:
            self.assertTrue(await asyncio.to_thread(deleting.wait, 2))
            sweeping.cancel()
            await asyncio.sleep(0)
            sweeping.cancel()
            producer = asyncio.create_task(self.fixture.execute(self.job_id))
            await asyncio.sleep(0)
            self.fixture.loader.assert_not_awaited()
            self.assertFalse(sweeping.done())
        finally:
            continue_delete.set()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(sweeping, 2)
        receipt = await asyncio.wait_for(producer, 2)
        self.assertEqual(receipt.job_id, self.job_id)
        self.assertIn(self.key, self.objects)

    async def test_new_accepted_attempt_does_not_pin_an_old_unused_key(self):
        fresh_key = config.storage.source_attempt_key_template.format(
            job_id=self.job_id, upload_token='b' * 32, sha256='a' * 64,
        )

        async def load(job_id):
            self.objects[fresh_key] = self.now
            return dict(submission(job_id)['request']['source'], key=fresh_key)

        self.fixture.loader.side_effect = load
        await self.fixture.execute(self.job_id)
        self.assertEqual(await self.janitor.sweep_orphan_sources(), 1)
        self.assertNotIn(self.key, self.objects)
        self.assertIn(fresh_key, self.objects)


class SourceDeleteAgeCheck(TestCase):
    def setUp(self):
        self.now = datetime.now(timezone.utc)
        self.job_id = 'source-age'
        self.key = f'uploads/{self.job_id}/' + 'b' * 64 + '.jpg'
        self.client = SimpleNamespace(
            stat_object=Mock(return_value=SimpleNamespace(last_modified=self.now)),
            remove_object=Mock(),
        )
        self.store = BotObjectStorage(None, None, None, 'imager', client=self.client)

    def test_current_modification_time_overrules_an_old_listing(self):
        self.assertFalse(self.store.delete_source(self.job_id, self.key, older_than=self.now - timedelta(hours=6)))
        self.client.remove_object.assert_not_called()

    def test_old_owned_source_can_be_deleted(self):
        self.client.stat_object.return_value.last_modified = self.now - timedelta(hours=7)
        self.assertTrue(self.store.delete_source(self.job_id, self.key, older_than=self.now - timedelta(hours=6)))
        self.client.remove_object.assert_called_once_with('imager', self.key)
