import asyncio
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage, SimpleEventIsolation
from aiogram.methods import EditMessageText, SendDocument, SendMessage
from aiogram.types import CallbackQuery, Chat, Document, Message, Update, User

from src.application.generation_result_menu import GenerationResultMenu
from src.commands.image_generation.image_generation_handler import register_handlers_image_generation
from src.commands.image_generation.image_generation_states import ImageGenerationStates as S
from src.commands.main_menu import register_handlers_main_menu, register_fallback
from src.infrastructure.services.imager_service.notifier import GenerationNotifier
from src.utils.navigation import NavigationMiddleware, STALE_VIEW_TEXT
from test_navigation_security import OfflineSession


class ResultSession(OfflineSession):
    def __init__(self):
        super().__init__()
        self.last = {}

    async def make_request(self, bot, method, timeout=None):
        if isinstance(method, SendDocument):
            self.requests.append(method)
            self.next_message += 1
            result = Message(
                message_id=self.next_message, date=datetime.now(timezone.utc),
                chat=Chat(id=method.chat_id, type='private'),
                from_user=User(id=bot.id, is_bot=True, first_name='IMager'),
                document=Document(file_id='result', file_unique_id='result', file_name='result.jpg'),
                caption=method.caption,
            ).as_(bot)
        else:
            result = await super().make_request(bot, method, timeout)
        if isinstance(result, Message):
            self.last[result.chat.id] = result
        return result


class GenerationResultMenuTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.session = ResultSession()
        self.bot = Bot(token='123456:OFFLINE_ONLY_TEST_TOKEN_000000000000', session=self.session)
        self.storage = MemoryStorage()
        self.dispatcher = Dispatcher(storage=self.storage, events_isolation=SimpleEventIsolation())
        self.user = SimpleNamespace(is_admin=False, isbanned=False)
        self.users = SimpleNamespace(get_user_by_id=AsyncMock(return_value=self.user))
        self.repositories = SimpleNamespace()
        self.dispatcher['user'] = self.user
        self.dispatcher['uow'] = self.repositories
        self.dispatcher.callback_query.outer_middleware(NavigationMiddleware())
        register_handlers_main_menu(self.dispatcher)
        register_handlers_image_generation(self.dispatcher, self.repositories)
        register_fallback(self.dispatcher)
        self.menu = GenerationResultMenu(self.dispatcher, self.users, self.bot)
        self.state = self.dispatcher.fsm.get_context(bot=self.bot, chat_id=5, user_id=5)
        self.job = {
            '_id': 'private-job', 'status': 'completed',
            'result': {'bucket': 'imager', 'key': 'synthetic'},
            'completion_outbox': {'token': 'send-token'},
            'request': {'client_context': {'user_id': 5, 'chat_id': 5}},
        }
        self.service = SimpleNamespace(
            config=SimpleNamespace(bot_notification_lease_seconds=120, telegram_max_document_size_bytes=100),
            claim_completion=AsyncMock(return_value=self.job),
            mark_delivery_started=AsyncMock(return_value=True), renew_completion=AsyncMock(return_value=True),
            acknowledge_completion=AsyncMock(return_value=True), fail_completion=AsyncMock(),
            object_store=SimpleNamespace(download_result=lambda ref, path, **kw: Path(path).write_bytes(b'result')),
        )
        self.notifier = GenerationNotifier(
            self.service, self.bot, SimpleNamespace(), on_result_delivered=self.menu,
        )

    async def asyncTearDown(self):
        await self.dispatcher.fsm.close()
        await self.bot.session.close()

    def callback(self, message, payload, *, actor=5):
        return CallbackQuery(
            id='menu-callback', chat_instance='private-offline-chat',
            from_user=User(id=actor, first_name='Test', is_bot=False),
            message=message, data=payload,
        ).as_(self.bot)

    async def test_result_is_followed_by_a_working_main_menu_with_secure_navigation(self):
        await self.notifier.deliver_once()
        sent = [request for request in self.session.requests if isinstance(request, (SendDocument, SendMessage))]
        self.assertEqual([type(request) for request in sent], [SendDocument, SendMessage])
        menu = self.session.last[5]
        labels = [button.text for row in menu.reply_markup.inline_keyboard for button in row]
        self.assertEqual(labels, ['Собрать изображение', 'О проекте', 'Поддержка'])
        button = menu.reply_markup.inline_keyboard[0][0]
        self.assertTrue(button.callback_data.startswith('n:'))
        await self.dispatcher.feed_update(self.bot, Update(
            update_id=1, callback_query=self.callback(menu, button.callback_data),
        ))
        self.assertEqual(await self.state.get_state(), S.choosing_generation_mode.state)
        labels = [button.text for row in self.session.last[5].reply_markup.inline_keyboard for button in row]
        self.assertIn('Из пресетов (реком.)', labels)
        self.assertIn('Вручную', labels)
        self.assertTrue(any(isinstance(request, EditMessageText) for request in self.session.requests))
        self.service.fail_completion.assert_not_awaited()

    async def test_result_menu_does_not_replace_an_active_new_form_or_draft(self):
        for state_value in (S.waiting_manual_image_size, None):
            with self.subTest(state=state_value):
                await self.state.clear()
                await self.state.set_state(state_value)
                await self.state.update_data(draft_id='new-draft', image_size=64)
                before = await self.state.get_data()
                self.assertFalse(await self.menu(self.job))
                self.assertEqual(await self.state.get_data(), before)
        self.assertEqual(self.session.requests, [])

    async def test_incoming_update_and_result_menu_share_the_same_isolation_lock(self):
        async with self.dispatcher.fsm.events_isolation.lock(key=self.state.key):
            pending = asyncio.create_task(self.menu(self.job))
            await asyncio.sleep(0)
            self.assertFalse(pending.done())
            await self.state.set_state(S.waiting_manual_image_size)
            await self.state.update_data(draft_id='newer-draft')
        self.assertFalse(await pending)
        self.assertEqual(self.session.requests, [])

    async def test_menu_failure_never_requeues_or_resends_the_result(self):
        self.notifier.on_result_delivered = AsyncMock(side_effect=OSError('menu storage unavailable'))
        await self.notifier.deliver_once()
        self.assertEqual(sum(isinstance(request, SendDocument) for request in self.session.requests), 1)
        self.service.acknowledge_completion.assert_awaited_once()
        self.service.fail_completion.assert_not_awaited()

    async def test_known_document_success_still_shows_menu_when_only_outbox_ack_fails(self):
        self.service.acknowledge_completion.side_effect = OSError('outbox unavailable')
        await self.notifier.deliver_once()
        self.assertEqual(self.session.last[5].text, 'Главное меню')
        self.service.fail_completion.assert_not_awaited()

    async def test_send_failure_does_not_show_a_success_menu(self):
        self.service.object_store.download_result = lambda *args, **kwargs: (_ for _ in ()).throw(OSError('MinIO down'))
        await self.notifier.deliver_once()
        self.assertEqual(self.session.requests, [])
        self.service.fail_completion.assert_awaited_once()

    async def test_another_actor_cannot_use_the_result_menu(self):
        await self.notifier.deliver_once()
        menu = self.session.last[5]
        button = menu.reply_markup.inline_keyboard[0][0]
        await self.dispatcher.feed_update(self.bot, Update(
            update_id=1, callback_query=self.callback(menu, button.callback_data, actor=6),
        ))
        self.assertEqual(self.session.requests[-1].text, STALE_VIEW_TEXT)
        self.assertIsNone(await self.state.get_state())

    async def test_fixed_admin_menu_uses_permanent_role(self):
        self.user.isbanned = True
        self.menu.fixed_admin_ids = frozenset({5})
        self.assertTrue(await self.menu(self.job))
        labels = [button.text for row in self.session.last[5].reply_markup.inline_keyboard for button in row]
        self.assertIn('Админ-панель', labels)

    async def test_large_telegram_ids_loaded_as_bson_int64_keep_working(self):
        from bson.int64 import Int64
        identifier = Int64(6000000000)
        self.job['request']['client_context'] = {'user_id': identifier, 'chat_id': identifier}
        self.assertTrue(await self.menu(self.job))
        self.assertEqual(self.session.last[int(identifier)].text, 'Главное меню')
        self.assertIs(type(self.users.get_user_by_id.await_args.args[0]), int)
