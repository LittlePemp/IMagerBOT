"""Private admin gallery through the real Dispatcher and network-free Telegram."""
import copy
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from bson import Int64
from aiogram.methods import EditMessageMedia, SendDocument, SendPhoto
from aiogram.types import CallbackQuery, Chat, Message, User as TelegramUser

from src.commands.admin_panel.recent_uploads import (
    PHOTO_MAX_BYTES, PREVIEW_KEY, list_recent_uploads,
)
from src.commands.admin_panel.ui import AdminCallback
from src.models.user import UserStatus
from src.utils.navigation import CURRENT_VIEW, STALE_VIEW_TEXT
import test_bot_routes as routes


def upload(index=0, *, content_type='image/jpeg'):
    job_id = f'00000000-0000-0000-0000-{index:012d}'
    return {
        '_id': job_id,
        'created_at': datetime(2026, 11, 23, 12, tzinfo=timezone.utc) - timedelta(minutes=index),
        'request': {
            'client_id': 'telegram', 'client_context': {'user_id': 3},
            'parameters': {'group_name': 'icons'},
            'source': {
                'bucket': 'private-bucket', 'key': f'uploads/{job_id}/source.jpg',
                'size_bytes': 5, 'sha256': 'a' * 64, 'content_type': content_type,
            },
        },
    }


class GallerySession(routes.FakeTelegramSession):
    def __init__(self):
        super().__init__()
        self.transfers = []
        self.media_failure = None

    async def make_request(self, bot, method, timeout=None):
        if not isinstance(method, (SendPhoto, SendDocument, EditMessageMedia)):
            return await super().make_request(bot, method, timeout)
        self.methods.append(method)
        if isinstance(method, EditMessageMedia):
            incoming = method.media.media
            message_id = method.message_id
        else:
            incoming = method.photo if isinstance(method, SendPhoto) else method.document
            self.next_message_id += 1
            message_id = self.next_message_id
        path = Path(incoming.path)
        with path.open('rb') as stream:
            prefix = stream.read(32)
        self.transfers.append({
            'path': path, 'filename': incoming.filename,
            'size': path.stat().st_size, 'prefix': prefix,
        })
        if self.media_failure is not None:
            raise self.media_failure
        message = Message(
            message_id=message_id, date=datetime.now(timezone.utc),
            chat=Chat(id=int(method.chat_id), type='private'),
            from_user=TelegramUser(id=bot.id, is_bot=True, first_name='IMager'),
            caption='Source preview',
        ).as_(bot)
        self.messages[(int(method.chat_id), message_id)] = message
        # Keep .last bound to the text menu, as a real view snapshot is.
        return message


class RecentUploadsAdminTests(unittest.IsolatedAsyncioTestCase):
    actor = routes.BotRouteTests.actor
    send = routes.BotRouteTests.send
    click = routes.BotRouteTests.click
    state = routes.BotRouteTests.state
    asyncTearDown = routes.BotRouteTests.asyncTearDown

    async def asyncSetUp(self):
        await routes.BotRouteTests.asyncSetUp(self)
        await self.session.close()
        self.session = GallerySession()
        self.bot.session = self.session
        self.rows = [upload(0), upload(1)]
        self.download_size = 5
        self.download_error = None
        self.revoke_after_download = False
        self.download_paths = []
        self.gateway = SimpleNamespace(
            recent_uploads=AsyncMock(side_effect=lambda **kwargs: copy.deepcopy(self.rows)),
            download_source=AsyncMock(side_effect=self._download),
        )
        self.dispatcher['recent_uploads_gateway'] = self.gateway

    async def _download(self, job_id, path):
        path = Path(path)
        self.download_paths.append(path)
        with path.open('wb') as stream:
            stream.write(b'image')
            stream.truncate(self.download_size)
        if self.revoke_after_download:
            self.people[2] = self.people[2].model_copy(update={'status': UserStatus.USER})
        if self.download_error is not None:
            raise self.download_error
        return str(path)

    async def open_list(self, actor_id=2):
        await self.send(actor_id, '/admin')
        return await self.click(actor_id, title='Последние 10 фото')

    def media_methods(self):
        return [method for method in self.session.methods
                if isinstance(method, (SendPhoto, SendDocument, EditMessageMedia))]

    def assert_sources_cleaned(self):
        self.assertTrue(self.download_paths)
        self.assertTrue(all(not path.exists() for path in self.download_paths))

    async def test_menu_uses_gateway_di_and_lists_newest_ten_without_source_refs(self):
        self.rows = [upload(index) for index in reversed(range(12))]
        self.people[3] = self.people[3].model_copy(update={
            'name': '<b>Имя</b>\n' + 'x' * 200,
            'telegram_username': 'bad<script>',
        })
        response = await self.open_list()
        self.gateway.recent_uploads.assert_awaited_once_with(limit=10)
        self.assertIn('Принятые изображения: 10 / 10', response.text)
        self.assertIn('23.11.2026 12:00', response.text)
        self.assertIn('ID 3', response.text)
        self.assertNotIn('@bad', response.text)
        self.assertLess(len(response.text), 4096)
        snapshot = await self.state(2).get_data()
        records = snapshot[CURRENT_VIEW]['records']
        self.assertEqual(records['0'], {'job_id': upload(0)['_id']})
        self.assertEqual(records['9'], {'job_id': upload(9)['_id']})
        self.assertNotIn('private-bucket', str(snapshot))
        self.assertNotIn('uploads/', str(snapshot))
        for row in response.reply_markup.inline_keyboard:
            for button in row:
                self.assertLessEqual(len(button.callback_data.encode()), 64)
                self.assertNotIn(upload(0)['_id'], button.callback_data)
        self.assertEqual(self.media_methods(), [])

    async def test_empty_list_has_refresh_and_back_without_media(self):
        self.rows = []
        response = await self.open_list()
        self.assertIn('Пока нет загруженных фото', response.text)
        buttons = [button.text for row in response.reply_markup.inline_keyboard for button in row]
        self.assertIn('Обновить', buttons)
        self.assertIn('Назад', buttons)
        await self.click(2, title='Обновить')
        self.assertEqual(self.gateway.recent_uploads.await_count, 2)
        self.gateway.download_source.assert_not_awaited()

    async def test_mongo_int64_actor_id_displays_resolved_name_username_and_id(self):
        actor_id = Int64(5_000_000_001)
        self.people[int(actor_id)] = self.people[3].model_copy(update={
            'telegram_id': int(actor_id), 'name': 'Большой ID',
            'telegram_username': 'large_id_user',
        })
        self.rows = [upload(0)]
        self.rows[0]['request']['client_context']['user_id'] = actor_id

        response = await self.open_list()

        self.assertIn('Большой ID (@large_id_user), ID 5000000001', response.text)
        resolved = [call.args[0] for call in self.users.get_user_by_id.await_args_list
                    if call.args[0] == actor_id]
        self.assertTrue(resolved)
        self.assertTrue(all(type(value) is int for value in resolved))

    async def test_non_admin_cannot_use_list_or_photo_callbacks(self):
        admin_menu = await self.send(2, '/admin')
        await self.click(3, title='Последние 10 фото', message=admin_menu)
        self.gateway.recent_uploads.assert_not_awaited()
        listing = await self.click(2, title='Последние 10 фото', message=admin_menu)
        await self.click(3, title='Открыть фото №1', message=listing)
        self.gateway.download_source.assert_not_awaited()
        self.assertEqual(self.media_methods(), [])

    async def test_gallery_callbacks_in_a_group_never_query_or_send_sources(self):
        private_menu = await self.send(2, '/admin')
        menu = private_menu.model_copy(update={
            'chat': Chat(id=-100100, type='group'),
        })
        await self.click(2, title='Последние 10 фото', message=menu)
        # The global middleware already drops groups; the gallery also enforces
        # private access itself if called from another dispatcher in the future.
        payload = next(button.callback_data for row in menu.reply_markup.inline_keyboard
                       for button in row if button.text == 'Последние 10 фото')
        await list_recent_uploads(CallbackQuery(
            id='direct-group-guard', from_user=self.actor(2), chat_instance='offline-group',
            message=menu, data=payload,
        ).as_(self.bot), self.state(2), self.gateway, self.users)
        self.gateway.recent_uploads.assert_not_awaited()
        self.gateway.download_source.assert_not_awaited()
        self.assertEqual(self.media_methods(), [])

    async def test_one_preview_is_edited_and_text_menu_stays_owner_bound(self):
        await self.open_list()
        await self.click(2, title='Открыть фото №1')
        preview = (await self.state(2).get_data())[PREVIEW_KEY]
        menu = self.session.last[2]
        self.assertIsNotNone(menu.text)
        self.assertNotEqual(preview['message_id'], menu.message_id)
        await self.click(2, title='Открыть фото №2')
        methods = self.media_methods()
        self.assertEqual([type(method) for method in methods], [SendPhoto, EditMessageMedia])
        self.assertEqual(methods[1].message_id, preview['message_id'])
        self.assertEqual(methods[0].chat_id, 2)
        self.assertEqual(methods[1].chat_id, 2)
        self.assertIsNone(methods[0].parse_mode)
        self.assertIsNone(methods[1].media.parse_mode)
        self.assert_sources_cleaned()

    async def test_same_old_callback_cannot_send_a_second_preview(self):
        listing = await self.open_list()
        callback = next(button.callback_data for row in listing.reply_markup.inline_keyboard
                        for button in row if button.text == 'Открыть фото №1')
        await self.click(2, message=listing, payload=callback)
        await self.click(2, message=listing, payload=callback)
        self.gateway.download_source.assert_awaited_once()
        self.assertEqual(len(self.media_methods()), 1)
        self.assertEqual(self.session.methods[-1].text, STALE_VIEW_TEXT)

    async def test_forged_record_or_unknown_row_cannot_redirect_a_source_download(self):
        listing = await self.open_list()
        data = await self.state(2).get_data()
        view = data[CURRENT_VIEW]
        view['records']['0'] = {
            'job_id': 'other-job', 'source': {'bucket': 'secrets', 'key': 'catalog/private'},
        }
        await self.state(2).update_data(**{CURRENT_VIEW: view})
        await self.click(2, title='Открыть фото №1', message=listing)
        self.gateway.download_source.assert_not_awaited()
        self.assertEqual(self.media_methods(), [])
        data = await self.state(2).get_data()
        forged = AdminCallback(view=data[CURRENT_VIEW]['token'], action='upload_show', item=999).pack()
        await self.click(2, payload=forged)
        self.gateway.download_source.assert_not_awaited()

    async def test_source_fields_in_record_are_ignored_for_an_allowed_job(self):
        listing = await self.open_list()
        data = await self.state(2).get_data()
        data[CURRENT_VIEW]['records']['0']['source'] = {
            'bucket': 'secrets', 'key': 'catalog/private',
        }
        await self.state(2).update_data(**{CURRENT_VIEW: data[CURRENT_VIEW]})
        await self.click(2, title='Открыть фото №1', message=listing)
        args = self.gateway.download_source.await_args.args
        self.assertEqual(args[0], upload(0)['_id'])
        self.assertIsInstance(args[1], Path)
        self.assertNotIn('catalog', str(args[1]))
        self.assertEqual(len(self.media_methods()), 1)

    async def test_revoked_role_after_download_prevents_transmitting_file(self):
        await self.open_list()
        self.revoke_after_download = True
        await self.click(2, title='Открыть фото №1')
        self.gateway.download_source.assert_awaited_once()
        self.assertEqual(self.media_methods(), [])
        self.assertIn('Недостаточно прав', self.session.last[2].text)
        self.assertEqual(await self.state(2).get_data(), {})
        self.assert_sources_cleaned()

    async def test_fresh_role_check_rejects_revoked_or_banned_admin(self):
        listing = await self.open_list()
        self.people[2] = self.people[2].model_copy(update={'isbanned': True})
        await self.click(2, title='Открыть фото №1', message=listing)
        self.gateway.download_source.assert_not_awaited()
        self.assertEqual(self.media_methods(), [])

    async def test_missing_or_retained_away_source_is_not_sent_or_retried(self):
        await self.open_list()
        self.download_error = FileNotFoundError('source removed by retention')
        await self.click(2, title='Открыть фото №1')
        self.gateway.download_source.assert_awaited_once()
        self.assertEqual(self.media_methods(), [])
        self.assertIn('Фото больше недоступно', self.session.last[2].text)
        self.assert_sources_cleaned()

    async def test_list_fetch_failure_can_be_refreshed_without_any_media(self):
        self.gateway.recent_uploads.side_effect = RuntimeError('Mongo unavailable')
        response = await self.open_list()
        self.assertIn('Не удалось загрузить список', response.text)
        self.assertNotIn('Пока нет загруженных фото', response.text)
        self.gateway.recent_uploads.side_effect = lambda **kwargs: copy.deepcopy(self.rows)
        response = await self.click(2, title='Обновить')
        self.assertIn('Принятые изображения: 2 / 10', response.text)
        self.assertEqual(self.media_methods(), [])

    async def test_webp_source_is_a_document_with_canonical_filename(self):
        self.rows = [upload(0, content_type='image/webp')]
        await self.open_list()
        await self.click(2, title='Открыть фото №1')
        self.assertEqual([type(method) for method in self.media_methods()], [SendDocument])
        self.assertTrue(self.session.transfers[0]['filename'].endswith('.webp'))
        self.assert_sources_cleaned()

    async def test_large_jpeg_uses_document_instead_of_exceeding_photo_limit(self):
        await self.open_list()
        self.download_size = PHOTO_MAX_BYTES + 1
        await self.click(2, title='Открыть фото №1')
        self.assertEqual([type(method) for method in self.media_methods()], [SendDocument])
        self.assertTrue(self.session.transfers[0]['filename'].endswith('.jpg'))
        self.assert_sources_cleaned()

    async def test_telegram_failure_does_not_trigger_an_automatic_second_send(self):
        await self.open_list()
        self.session.media_failure = TimeoutError('unknown Telegram send outcome')
        await self.click(2, title='Открыть фото №1')
        self.assertEqual(len(self.media_methods()), 1)
        self.gateway.download_source.assert_awaited_once()
        self.assertIn('Не удалось показать фото', self.session.last[2].text)
        self.assert_sources_cleaned()
