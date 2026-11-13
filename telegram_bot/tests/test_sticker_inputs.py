"""Sticker selection stays in Telegram; decoding stays in the image worker."""
import unittest
import time
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiogram.types import Chat, Message, PhotoSize, Sticker, Update

from imager_config.loader import config
from imager_contracts.protocol import BINARY_CONTENT_TYPE, JPEG_CONTENT_TYPE, WEBP_CONTENT_TYPE
from src.application.image_errors import IMAGE_TOO_NARROW_TEXT, IMAGE_TOO_WIDE_TEXT
from src.commands.image_generation.image_generation_states import ImageGenerationStates
from src.commands.image_generation.upload import (
    MISSING_STICKER_PREVIEW_TEXT, TelegramImageSource, generation_job_id,
)
from src.infrastructure.services.object_storage import BotObjectStorage
import test_bot_routes as bot_routes
from test_temporal_boundary import Minio


def sticker(*, animated=False, video=False, preview=True, width=512, height=512):
    return Sticker(
        file_id='original-sticker', file_unique_id='sticker-unique', type='regular',
        width=width, height=height, file_size=100,
        is_animated=animated, is_video=video,
        thumbnail=PhotoSize(
            file_id='static-preview', file_unique_id='preview-unique', width=256, height=256,
            file_size=50,
        ) if preview else None,
    )


class TestStickerRoutes(unittest.IsolatedAsyncioTestCase):
    # Reuse the real, network-free Dispatcher fixture without rerunning its tests.
    asyncSetUp = bot_routes.BotRouteTests.asyncSetUp
    asyncTearDown = bot_routes.BotRouteTests.asyncTearDown
    actor = bot_routes.BotRouteTests.actor
    state = bot_routes.BotRouteTests.state

    async def prepare(self):
        await self.state(3).set_state(ImageGenerationStates.waiting_for_image_upload)
        await self.state(3).set_data({
            'image_group': 'icons', 'image_size': 64, 'inset_size': 40, 'noise_level': 30,
            '_current_view': {'expires_at': time.time() + 60},
        })
        self.accepted = AsyncMock()
        self.progress = AsyncMock()
        self.dispatcher['generation_service'] = SimpleNamespace(execute=self.accepted, gateway=object())
        self.dispatcher['generation_progress'] = SimpleNamespace(watch=self.progress)

    async def send_media(self, *, media=None, photo=None, advance=2):
        self.sequence += 1
        self.clock += advance
        message = Message(
            message_id=self.sequence, date=datetime.now(timezone.utc),
            chat=Chat(id=3, type='private'), from_user=self.actor(3), sticker=media, photo=photo,
        ).as_(self.bot)
        self.accepted.return_value = SimpleNamespace(job_id=generation_job_id(message), duplicate=False)
        await self.dispatcher.feed_update(self.bot, Update(update_id=self.sequence, message=message))
        return message

    async def test_static_animated_and_video_stickers_share_the_durable_submission_route(self):
        for animated, video in ((False, False), (True, False), (False, True)):
            with self.subTest(animated=animated, video=video):
                await self.prepare()
                # Original animation dimensions must not reject its valid preview.
                media = sticker(animated=animated, video=video,
                                width=100 if animated or video else 512)
                message = await self.send_media(media=media, advance=config.bot.upload_cooldown_seconds + 1)
                self.accepted.assert_awaited_once()
                arguments = self.accepted.await_args.kwargs
                self.assertEqual(arguments['job_id'], generation_job_id(message))
                self.assertEqual(arguments['actor_id'], 3)
                image, mime = arguments['source_loader'].validate()
                self.assertEqual(image.file_id, 'static-preview' if animated or video else 'original-sticker')
                self.assertEqual(mime, BINARY_CONTENT_TYPE if animated or video else WEBP_CONTENT_TYPE)
                self.assertEqual(arguments['draft']['image_group'], 'icons')
                self.progress.assert_awaited_once()
                self.assertIsNone(await self.state(3).get_state())

    async def test_sticker_without_preview_is_rejected_before_admission_and_keeps_draft(self):
        for animated, video in ((True, False), (False, True)):
            with self.subTest(animated=animated, video=video):
                await self.prepare()
                await self.send_media(media=sticker(animated=animated, video=video, preview=False),
                                      advance=config.bot.upload_cooldown_seconds + 1)
                self.assertEqual(self.session.last[3].text, MISSING_STICKER_PREVIEW_TEXT)
                self.accepted.assert_not_awaited()
                self.progress.assert_not_awaited()
                self.assertEqual(await self.state(3).get_state(), ImageGenerationStates.waiting_for_image_upload.state)
                self.assertEqual((await self.state(3).get_data())['image_group'], 'icons')

    async def test_narrow_and_wide_photo_or_sticker_get_friendly_preflight_without_quota_charge(self):
        for as_sticker in (False, True):
            for width, height, expected in ((100, 512, IMAGE_TOO_NARROW_TEXT), (512, 100, IMAGE_TOO_WIDE_TEXT)):
                with self.subTest(sticker=as_sticker, width=width, height=height):
                    await self.prepare()
                    if as_sticker:
                        await self.send_media(media=sticker(width=width, height=height),
                                              advance=config.bot.upload_cooldown_seconds + 1)
                    else:
                        await self.send_media(photo=[PhotoSize(
                            file_id='photo', file_unique_id='photo-unique', width=width, height=height,
                        )], advance=config.bot.upload_cooldown_seconds + 1)
                    self.assertEqual(self.session.last[3].text, expected)
                    self.accepted.assert_not_awaited()  # admission and quota never start
                    self.progress.assert_not_awaited()
                    self.assertEqual(await self.state(3).get_state(), ImageGenerationStates.waiting_for_image_upload.state)

    async def test_sticker_uploads_have_the_same_cooldown_as_photos(self):
        await self.prepare()
        await self.send_media(media=sticker())
        await self.state(3).set_state(ImageGenerationStates.waiting_for_image_upload)
        await self.send_media(media=sticker(), advance=0.01)
        self.accepted.assert_awaited_once()
        self.assertIn('Слишком часто', self.session.last[3].text)


class TestStickerTransfer(unittest.IsolatedAsyncioTestCase):
    async def test_selected_file_is_transferred_unchanged_with_accurate_content_type(self):
        from src.commands.image_generation import upload

        for animated, video in ((False, False), (True, False), (False, True)):
            with self.subTest(animated=animated, video=video):
                payload = b'original-static-image'
                uploaded = []

                async def download(remote, destination, **kwargs):
                    destination.write(payload)

                async def store(path, job_id, *, content_type):
                    uploaded.append((path.read_bytes(), job_id, content_type))
                    return {'uploaded': True}

                bot = SimpleNamespace(
                    get_file=AsyncMock(return_value=SimpleNamespace(file_path='stickers/source', file_size=len(payload))),
                    download_file=AsyncMock(side_effect=download),
                )
                message = SimpleNamespace(sticker=sticker(animated=animated, video=video), bot=bot)
                gateway = SimpleNamespace(upload_source=AsyncMock(side_effect=store))
                with patch.object(upload.shutil, 'disk_usage', return_value=SimpleNamespace(free=10**12)):
                    self.assertEqual(await TelegramImageSource(message, gateway)('accepted-job'), {'uploaded': True})
                bot.get_file.assert_awaited_once_with('static-preview' if animated or video else 'original-sticker')
                self.assertEqual(uploaded, [(payload, 'accepted-job',
                                           BINARY_CONTENT_TYPE if animated or video else WEBP_CONTENT_TYPE)])

    async def test_preview_metadata_is_validated_before_downloading_the_selected_file(self):
        media = sticker(animated=True)
        media = media.model_copy(update={
            'thumbnail': media.thumbnail.model_copy(update={'width': 100, 'height': 512}),
        })
        bot = SimpleNamespace(get_file=AsyncMock(), download_file=AsyncMock())
        gateway = SimpleNamespace(upload_source=AsyncMock())
        with self.assertRaisesRegex(ValueError, 'слишком узкая'):
            await TelegramImageSource(SimpleNamespace(sticker=media, bot=bot), gateway)('job')
        bot.get_file.assert_not_awaited()
        gateway.upload_source.assert_not_awaited()

    async def test_oversized_sticker_or_preview_cannot_bypass_source_size_limit(self):
        for animated in (False, True):
            with self.subTest(animated=animated):
                media = sticker(animated=animated)
                if animated:
                    media = media.model_copy(update={
                        'thumbnail': media.thumbnail.model_copy(update={'file_size': config.limits.source_file_bytes + 1}),
                    })
                else:
                    media = media.model_copy(update={'file_size': config.limits.source_file_bytes + 1})
                bot = SimpleNamespace(get_file=AsyncMock())
                with self.assertRaisesRegex(ValueError, 'Размер исходного файла'):
                    await TelegramImageSource(SimpleNamespace(sticker=media, bot=bot), object())('job')
                bot.get_file.assert_not_awaited()


class TestSourceContentType(unittest.TestCase):
    def test_source_mime_and_checksum_survive_the_minio_boundary(self):
        import hashlib

        class CapturingMinio(Minio):
            def fput_object(self, bucket, key, path, **kwargs):
                self.content_type = kwargs['content_type']
                super().fput_object(bucket, key, path, **kwargs)

        for mime in (JPEG_CONTENT_TYPE, WEBP_CONTENT_TYPE, BINARY_CONTENT_TYPE):
            with self.subTest(content_type=mime), TemporaryDirectory() as directory:
                payload = b'unchanged-source'
                path = Path(directory) / 'source.bin'
                path.write_bytes(payload)
                client = CapturingMinio()
                storage = BotObjectStorage('', '', '', 'imager', client=client)
                reference = storage.upload_source(path, 'job', content_type=mime)
                self.assertEqual(reference['content_type'], mime)
                self.assertEqual(client.content_type, mime)
                self.assertEqual(reference['sha256'], hashlib.sha256(payload).hexdigest())
                self.assertEqual(client.objects[('imager', reference['key'])], payload)
