"""Shared Local Bot API files stay readable until every transfer finishes."""
import asyncio
from dataclasses import replace
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from imager_config.loader import config
from imager_contracts.scratch import purge_telegram_cache
from src.commands.admin_panel.pack_import import handlers
from src.commands.image_generation import upload
from src.infrastructure.services import telegram_files


class TestTelegramCacheLeases(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.path = self.root / 'fake-token' / 'photos' / 'same.jpg'
        self.path.parent.mkdir(parents=True)
        self.payload = b'unchanged encoded source'
        self.path.write_bytes(self.payload)
        self.config = replace(config, pack_import=replace(
            config.pack_import, bot_api_container_directory=str(self.root)))
        self.bot = SimpleNamespace(session=SimpleNamespace(api=SimpleNamespace(is_local=True)))

    def configure(self):
        patches = [patch.object(module, 'config', self.config)
                   for module in (telegram_files, upload, handlers)]
        for current in patches:
            current.start()
            self.addCleanup(current.stop)
        disk = patch.object(upload.shutil, 'disk_usage', return_value=SimpleNamespace(free=10**12))
        disk.start()
        self.addCleanup(disk.stop)

    async def test_photo_and_sticker_can_read_the_same_cached_file_concurrently(self):
        self.configure()
        for sticker in (False, True):
            with self.subTest(sticker=sticker):
                self.path.write_bytes(self.payload)
                second_resolved, first_finished = asyncio.Event(), asyncio.Event()
                uploaded = []
                path, payload = self.path, self.payload

                class Bot:
                    session = self.bot.session

                    def __init__(self, second):
                        self.second = second

                    async def get_file(self, file_id):
                        if self.second:
                            second_resolved.set()
                        return SimpleNamespace(file_path=str(path), file_size=len(payload))

                    async def download_file(self, file_path, destination, *, timeout):
                        if self.second:
                            await first_finished.wait()
                        with Path(file_path).open('rb') as source:
                            destination.write(source.read())

                async def store(path, job_id, *, content_type):
                    if job_id == 'first':
                        await second_resolved.wait()
                    uploaded.append((job_id, path.read_bytes()))
                    return {'job': job_id}

                def message(second):
                    image = SimpleNamespace(file_id='same', width=100, height=100,
                                            file_size=len(payload), is_animated=False, is_video=False)
                    return SimpleNamespace(bot=Bot(second), sticker=image if sticker else None,
                                           photo=[] if sticker else [image])

                gateway = SimpleNamespace(upload_source=store)

                async def first():
                    result = await upload.TelegramImageSource(message(False), gateway)('first')
                    self.assertTrue(path.exists())  # The second reader still owns it.
                    first_finished.set()
                    return result

                result = await asyncio.wait_for(asyncio.gather(
                    first(), upload.TelegramImageSource(message(True), gateway)('second')), timeout=5)
                self.assertEqual(result, [{'job': 'first'}, {'job': 'second'}])
                self.assertEqual(uploaded, [('first', payload), ('second', payload)])
                self.assertFalse(path.exists())

    async def test_zip_imports_share_a_reader_pin_without_a_second_archive_copy(self):
        self.configure()
        self.path = self.root / 'fake-token' / 'documents' / 'same.zip'
        self.path.parent.mkdir()
        self.path.write_bytes(self.payload)
        second_resolved, first_finished = asyncio.Event(), asyncio.Event()
        self.bot.get_file = AsyncMock(return_value=SimpleNamespace(
            file_path=str(self.path), file_size=len(self.payload)))

        async def store(path, import_id, *, expected_size):
            self.assertEqual(path, self.path)
            if import_id == 'second':
                second_resolved.set()
                await first_finished.wait()
            else:
                await second_resolved.wait()
            self.assertEqual(path.read_bytes(), self.payload)
            return {'import_id': import_id}

        service = SimpleNamespace(archive_limit=config.pack_import.max_archive_bytes, upload_archive=store)

        async def transfer(import_id):
            result = await handlers._upload_archive_from_telegram(
                SimpleNamespace(bot=self.bot), {'file_size':len(self.payload), 'file_id':'same',
                                                'import_id':import_id}, service)
            if import_id == 'first':
                self.assertTrue(self.path.exists())
                first_finished.set()
            return result

        with (patch.object(handlers, 'settings', SimpleNamespace(telegram_api_local=True)),
              patch.object(handlers.tempfile, 'TemporaryDirectory', side_effect=AssertionError('No ZIP copy'))):
            result = await asyncio.wait_for(asyncio.gather(transfer('first'), transfer('second')), timeout=5)
        self.assertEqual(result, [{'import_id':'first'}, {'import_id':'second'}])
        self.assertFalse(self.path.exists())

    async def test_janitor_respects_unresolved_get_file_then_only_the_bound_reader(self):
        self.configure()
        other = self.path.with_name('unused.jpg')
        other.write_bytes(b'other')
        old = time.time() - config.bot.telegram_cache_max_age_seconds - 60
        import os
        for path in (self.path, other):
            os.utime(path, (old, old))
        with telegram_files.local_file_transfer(self.bot) as transfer:
            self.assertEqual(await asyncio.to_thread(
                purge_telegram_cache, self.root, config.bot.telegram_cache_max_age_seconds), (0, 0))
            transfer.bind(self.path)
            self.assertEqual(await asyncio.to_thread(
                purge_telegram_cache, self.root, config.bot.telegram_cache_max_age_seconds), (1, 5))
            self.assertTrue(self.path.exists())
        self.assertEqual(await asyncio.to_thread(
            purge_telegram_cache, self.root, config.bot.telegram_cache_max_age_seconds), (1, len(self.payload)))

    async def test_failed_get_file_releases_its_root_pin(self):
        self.configure()
        self.bot.get_file = AsyncMock(side_effect=ConnectionError('getFile unavailable'))
        image = SimpleNamespace(file_id='same', width=100, height=100, file_size=len(self.payload))
        with self.assertRaises(ConnectionError):
            await upload.TelegramImageSource(SimpleNamespace(bot=self.bot, sticker=None, photo=[image]),
                                             SimpleNamespace())('unaccepted')
        self.assertEqual(await asyncio.to_thread(purge_telegram_cache, self.root, -1),
                         (1, len(self.payload)))
        self.path.write_bytes(self.payload)
        # A failed unresolved getFile also unblocks an earlier deferred release.
        with self.assertRaises(ConnectionError):
            with telegram_files.local_file_transfer(self.bot):
                self.assertFalse(telegram_files.release_local_file(self.bot, str(self.path)))
                raise ConnectionError('getFile unavailable')
        self.assertFalse(self.path.exists())

    async def test_cancelled_get_file_releases_its_root_pin(self):
        self.configure()
        started = asyncio.Event()

        async def get_file(file_id):
            started.set()
            await asyncio.Event().wait()

        self.bot.get_file = get_file
        image = SimpleNamespace(file_id='same', width=100, height=100, file_size=len(self.payload))
        task = asyncio.create_task(upload.TelegramImageSource(
            SimpleNamespace(bot=self.bot, sticker=None, photo=[image]), SimpleNamespace())('unaccepted'))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(await asyncio.to_thread(purge_telegram_cache, self.root, -1),
                         (1, len(self.payload)))

    async def test_deferred_delete_failure_keeps_transfer_success_and_redacts_token(self):
        self.configure()
        failure = PermissionError(13, 'permission denied', str(self.path))
        with patch.object(telegram_files.exception_logger, 'error') as log:
            with patch.object(Path, 'unlink', side_effect=failure):
                with telegram_files.local_file_transfer(self.bot) as transfer:
                    transfer.bind(self.path)
                    self.assertFalse(telegram_files.release_local_file(self.bot, str(self.path)))
            self.assertTrue(self.path.exists())
            log.assert_called_once()
            text = str(log.call_args)
            self.assertIn('PermissionError', text)
            self.assertNotIn('fake-token', text)
