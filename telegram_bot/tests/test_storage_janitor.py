import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from imager_config.loader import config
from src.application.source_lifecycle import SourceLifecycle
from imager_contracts.scratch import (
    purge_stale_entries, purge_telegram_cache, release_telegram_file,
)
from src.infrastructure.services.imager_service.notifier import GenerationNotifier
from src.infrastructure.services.object_storage import BotObjectStorage
from src.infrastructure.services.storage_janitor import StorageJanitor
from src.infrastructure.services.telegram_files import release_local_file
from test_delivery_policy import job_document

JOB = '0b2f6c0e-7d41-5a3c-9c55-6f3a2d1e4b10'


def touch(path, data=b'x', age=0):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    moment = time.time() - age
    os.utime(path, (moment, moment))
    return path


class TestScratchAndTelegramCache(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.addCleanup(self.temporary.cleanup)

    def test_only_old_prefixed_scratch_entries_are_removed(self):
        old_dir = self.root / 'imager-upload-old'
        touch(old_dir / 'source.jpg')
        os.utime(old_dir, (time.time() - 10_000,) * 2)
        touch(self.root / 'imager-upload-new' / 'source.jpg')
        touch(self.root / 'foreign-old', age=10_000)
        removed = purge_stale_entries(self.root, ('imager-upload-',), 3600)
        self.assertEqual(removed, 1)
        self.assertEqual(sorted(item.name for item in self.root.iterdir()), ['foreign-old', 'imager-upload-new'])

    def test_telegram_cache_keeps_databases_and_recent_files(self):
        bot = self.root / '123_token'  # real dirs are named by the bot token
        old_photo = touch(bot / 'photos' / 'file_1.jpg', b'p' * 10, age=10_000)
        old_zip = touch(bot / 'documents' / 'pack.zip', b'z' * 20, age=10_000)
        recent = touch(bot / 'photos' / 'file_2.jpg', age=10)
        database = touch(bot / 'td.binlog', age=10_000)
        unknown = touch(bot / 'custom' / 'keep.bin', age=10_000)
        files, released = purge_telegram_cache(self.root, 3600)
        self.assertEqual((files, released), (2, 30))
        self.assertFalse(old_photo.exists() or old_zip.exists())
        self.assertTrue(recent.exists() and database.exists() and unknown.exists())

    def test_release_accepts_only_media_inside_the_cache(self):
        photo = touch(self.root / 'bot' / 'photos' / 'a.jpg')
        self.assertTrue(release_telegram_file(self.root, photo))
        self.assertFalse(release_telegram_file(self.root, photo))
        for path in (self.root / 'bot' / 'td.binlog', self.root.parent / 'elsewhere.jpg'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                release_telegram_file(self.root, path)

    def test_bot_releases_only_in_local_mode_and_never_raises(self):
        photo = touch(self.root / 'bot' / 'photos' / 'a.jpg')
        cloud = SimpleNamespace(session=SimpleNamespace(api=SimpleNamespace(is_local=False)))
        local = SimpleNamespace(session=SimpleNamespace(api=SimpleNamespace(is_local=True)))
        directory = replace_directory(self.root)
        with directory:
            self.assertFalse(release_local_file(cloud, str(photo)))
            self.assertTrue(photo.exists())
            self.assertTrue(release_local_file(local, str(photo)))
            self.assertFalse(photo.exists())
            self.assertFalse(release_local_file(local, str(self.root / 'bot' / 'td.binlog')))


def replace_directory(root):
    from dataclasses import replace
    patched = replace(config, pack_import=replace(config.pack_import, bot_api_container_directory=str(root)))
    return patch('src.infrastructure.services.telegram_files.config', patched)


class TestSourceKeys(unittest.TestCase):
    def test_source_job_identity_is_parsed_only_from_the_source_layout(self):
        self.assertEqual(BotObjectStorage.source_job_id(f'uploads/{JOB}/{"a" * 64}.jpg'), JOB)
        self.assertEqual(BotObjectStorage.source_job_id(f'uploads/{JOB}/source.jpg'), JOB)
        for key in ('catalog/images/x.png', f'jobs/{JOB}/attempts/t/result.jpg', 'uploads/../x', 'uploads/'):
            with self.subTest(key=key):
                self.assertIsNone(BotObjectStorage.source_job_id(key))

    def test_delete_refuses_a_key_of_another_job(self):
        client = SimpleNamespace(remove_object=lambda *args: None)
        store = BotObjectStorage(None, None, None, 'imager', client=client)
        with self.assertRaises(ValueError):
            store.delete_source('other-job', f'uploads/{JOB}/source.jpg')


class TestStorageJanitor(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 24, 12, tzinfo=timezone.utc)
        old = self.now - timedelta(seconds=config.bot.orphan_source_grace_seconds + 60)
        self.keys = {
            'orphan': (f'uploads/{JOB}/{"a" * 64}.jpg', old),
            'admitted': ('uploads/admitted-job/source.jpg', old),
            'core': ('uploads/core-job/source.jpg', old),
            'fresh': ('uploads/fresh-job/source.jpg', self.now),
        }
        self.gateway = SimpleNamespace(
            list_sources=AsyncMock(return_value=list(self.keys.values())),
            get_job=AsyncMock(side_effect=lambda job_id: {'_id': job_id} if job_id == 'core-job' else None),
            source_bucket='imager',
            source_is_referenced=AsyncMock(side_effect=lambda key: key == self.keys['core'][0]),
            delete_source=AsyncMock(return_value=True),
        )
        self.admissions = SimpleNamespace(
            find=AsyncMock(side_effect=lambda job_id: object() if job_id == 'admitted-job' else None),
            source_is_referenced=AsyncMock(side_effect=lambda bucket, key: key == self.keys['admitted'][0]),
        )

    async def test_only_unreferenced_sources_past_grace_are_deleted(self):
        janitor = StorageJanitor(self.gateway, self.admissions, local_api=False,
                                 source_lifecycle=SourceLifecycle(), clock=lambda: self.now)
        self.assertEqual(await janitor.sweep_orphan_sources(), 1)
        self.gateway.delete_source.assert_awaited_once_with(
            JOB, self.keys['orphan'][0],
            older_than=self.now - timedelta(seconds=config.bot.orphan_source_grace_seconds),
        )

    async def test_cache_sweep_is_skipped_without_local_bot_api(self):
        janitor = StorageJanitor(self.gateway, self.admissions, local_api=False,
                                 source_lifecycle=SourceLifecycle(), clock=lambda: self.now)
        self.assertEqual(await janitor.sweep_telegram_cache(), (0, 0))

    async def test_one_failing_step_does_not_stop_the_others(self):
        self.gateway.list_sources.side_effect = OSError('minio down')
        janitor = StorageJanitor(self.gateway, self.admissions, local_api=False,
                                 source_lifecycle=SourceLifecycle(), clock=lambda: self.now)
        report = await janitor.run_once()
        self.assertNotIn('orphan_sources', report)
        self.assertEqual(report['telegram_cache'], (0, 0))


class TestFailedGenerationRefund(unittest.IsolatedAsyncioTestCase):
    def notifier(self, job, admissions):
        service = SimpleNamespace(
            claim_history_projection=AsyncMock(return_value=job),
            acknowledge_history_projection=AsyncMock(return_value=True),
            fail_history_projection=AsyncMock(),
        )
        history = SimpleNamespace(create_record=AsyncMock(return_value=job['request']['client_context']['history_record_id']),
                                  update_record=AsyncMock(return_value=True))
        return GenerationNotifier(service, None, history, admissions=admissions), service

    async def test_failed_generation_returns_quota_unit(self):
        job = job_document()
        job.update(status='failed', result=None, error='worker crashed')
        admissions = SimpleNamespace(refund=AsyncMock(return_value=True))
        notifier, service = self.notifier(job, admissions)
        await notifier.repair_history_once()
        admissions.refund.assert_awaited_once_with('job-1')
        service.acknowledge_history_projection.assert_awaited_once()

    async def test_completed_generation_keeps_its_quota_unit(self):
        admissions = SimpleNamespace(refund=AsyncMock())
        notifier, _ = self.notifier(job_document(), admissions)
        await notifier.repair_history_once()
        admissions.refund.assert_not_awaited()

    async def test_refund_failure_is_retried_through_the_projection(self):
        job = job_document()
        job.update(status='failed', result=None)
        admissions = SimpleNamespace(refund=AsyncMock(side_effect=OSError('mongo down')))
        notifier, service = self.notifier(job, admissions)
        await notifier.repair_history_once()
        service.fail_history_projection.assert_awaited_once()
        service.acknowledge_history_projection.assert_not_awaited()
