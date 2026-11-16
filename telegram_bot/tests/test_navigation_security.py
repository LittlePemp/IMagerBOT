import asyncio
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiogram import Bot, Dispatcher, F
from aiogram.client.session.base import BaseSession
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage, SimpleEventIsolation
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage
from aiogram.types import (
    CallbackQuery, Chat, InaccessibleMessage, InlineKeyboardButton,
    InlineKeyboardMarkup, Message, Update, User as TelegramUser,
)

from imager_config.loader import config
from settings import settings
from src.commands.admin_panel.ui import AdminCallback, command
from src.models.user import User, UserStatus
from src.utils.middlewares.abuse_control import (
    AbuseControlMiddleware, RequestLimiter, UploadBusy, UploadGate,
)
from src.utils.middlewares.authentication_middleware import AuthenticationMiddleware
from src.utils.middlewares.errors import ErrorBoundaryMiddleware
from src.utils.navigation import (
    CURRENT_VIEW, STALE_VIEW_TEXT, NavigationCallback, NavigationMiddleware,
    bind_view, render_view, require_current,
)


class OfflineSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.requests = []
        self.next_message = 100

    async def make_request(self, bot, method, timeout=None):
        self.requests.append(method)
        if isinstance(method, AnswerCallbackQuery):
            return True
        if isinstance(method, (SendMessage, EditMessageText)):
            self.next_message += 1
            return Message(
                message_id=method.message_id if isinstance(method, EditMessageText) else self.next_message,
                date=datetime.now(timezone.utc),
                chat=Chat(id=method.chat_id, type='private'),
                from_user=TelegramUser(id=bot.id, is_bot=True, first_name='Offline bot'),
                text=method.text, reply_markup=method.reply_markup,
            ).as_(bot)
        raise AssertionError(f'Unexpected Telegram operation: {type(method).__name__}')

    async def stream_content(self, *args, **kwargs):
        raise AssertionError('A test attempted a network download')
        yield b''

    async def close(self):
        pass


class TelegramFixture(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.session = OfflineSession()
        self.bot = Bot(token='123456:OFFLINE_ONLY_TEST_TOKEN_000000000000', session=self.session)
        self.storage = MemoryStorage()
        self.state = FSMContext(
            self.storage, StorageKey(bot_id=self.bot.id, chat_id=5, user_id=5),
        )

    async def asyncTearDown(self):
        await self.storage.close()
        await self.bot.session.close()

    def message(self, *, actor=5, message_id=10, chat_type='private', username=None):
        return Message(
            message_id=message_id, date=datetime.now(timezone.utc),
            chat=Chat(id=5, type=chat_type),
            from_user=TelegramUser(id=actor, is_bot=False, first_name='Test', username=username),
            text='test',
        ).as_(self.bot)

    def callback(self, raw, *, actor=5, message_id=10):
        return CallbackQuery(
            id=f'callback-{len(self.session.requests)}', chat_instance='offline-chat',
            from_user=TelegramUser(id=actor, is_bot=False, first_name='Test'),
            message=self.message(message_id=message_id), data=raw,
        ).as_(self.bot)


class TestViewBinding(TelegramFixture):
    async def test_old_nonce_is_rejected_after_same_message_is_redrawn(self):
        message = self.message()
        await bind_view(self.state, message, owner_id=5, token='view-a')
        await bind_view(self.state, message, owner_id=5, token='view-b')
        self.assertFalse(await require_current(self.callback('unused'), self.state, 'view-a'))
        self.assertTrue(await require_current(self.callback('unused'), self.state, 'view-b'))
        self.assertEqual(self.session.requests[-1].text, STALE_VIEW_TEXT)

    async def test_button_must_belong_to_actor_and_message(self):
        await bind_view(self.state, self.message(), owner_id=5, token='current')
        self.assertFalse(await require_current(self.callback('unused', actor=6), self.state, 'current'))
        self.assertFalse(await require_current(self.callback('unused', message_id=11), self.state, 'current'))
        self.assertTrue(await require_current(self.callback('unused'), self.state, 'current'))

    async def test_current_view_survives_old_expiry_metadata_but_inaccessible_view_is_rejected(self):
        await bind_view(self.state, self.message(), owner_id=5, token='current')
        current = (await self.state.get_data())[CURRENT_VIEW]
        self.assertNotIn('expires_at', current)
        self.assertNotIn('expires_on', current)
        # Documents from previous releases can retain their old timestamp until
        # the startup migration runs. It must not invalidate the current nonce.
        current.update(expires_at=100, expires_on=datetime(1970, 1, 1, tzinfo=timezone.utc))
        await self.state.update_data(**{CURRENT_VIEW: current})
        self.assertTrue(await require_current(self.callback('unused'), self.state, 'current'))
        inaccessible = self.callback('unused').model_copy(update={
            'message': InaccessibleMessage(chat=Chat(id=5, type='private'), message_id=10, date=0),
        })
        self.assertFalse(await require_current(inaccessible, self.state, 'current'))

    async def test_long_unicode_action_stays_on_server_and_wire_callback_fits(self):
        action = 'image_group/' + 'Ж' * 128
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text='Библиотека', callback_data=action),
        ]])
        sent = await render_view(self.message(), self.state, 'Select', keyboard)
        packed = sent.reply_markup.inline_keyboard[0][0].callback_data
        self.assertLessEqual(len(packed.encode()), 64)
        self.assertNotIn('Ж', packed)
        handler = AsyncMock()
        await NavigationMiddleware()(handler, self.callback(packed, message_id=sent.message_id), {'state': self.state})
        self.assertEqual(handler.await_args.args[0].data, action)

    async def test_malformed_unknown_or_unbound_callbacks_cannot_reach_handler(self):
        await bind_view(self.state, self.message(), owner_id=5, token='current', records={'0': 'allowed'})
        handler = AsyncMock()
        for raw in ('main_menu', 'n:broken', 'n:current:NaN', 'n:current:500', 'n:old:0'):
            await NavigationMiddleware()(handler, self.callback(raw), {'state': self.state})
        handler.assert_not_awaited()
        self.assertEqual(len(self.session.requests), 5)

    async def test_admin_command_checks_current_screen_before_resolving_target(self):
        await self.state.update_data(admin_context={'page': 1})
        await bind_view(self.state, self.message(), owner_id=5, token='current', records={
            '0': {'user_id': 77, 'revision': 2},
        })
        packed = AdminCallback(view='current', action='ban', item=0).pack()
        action = await command(self.callback(packed), self.state)
        self.assertEqual(action.record, {'user_id': 77, 'revision': 2})
        self.assertEqual(action.context, {'page': 1})
        self.assertIsNone(await command(self.callback(
            AdminCallback(view='old', action='ban', item=0).pack()), self.state))

    async def test_real_dispatcher_decodes_navigation_and_preserves_admin_callbacks(self):
        dispatcher = Dispatcher(storage=self.storage, events_isolation=SimpleEventIsolation())
        dispatcher.callback_query.outer_middleware(NavigationMiddleware())
        hits = []

        async def navigate(callback):
            hits.append(callback.data)
            await callback.answer()

        async def admin(callback, state):
            action = await command(callback, state)
            if action is not None:
                hits.append(action.action)

        async def fallback(callback):
            await callback.answer(STALE_VIEW_TEXT)

        # Handler argument names are those supplied by aiogram DI.
        dispatcher.callback_query.register(navigate, F.data == 'allowed')
        dispatcher.callback_query.register(admin, AdminCallback.filter(F.action == 'ban'))
        dispatcher.callback_query.register(fallback)
        await bind_view(self.state, self.message(), owner_id=5, token='current', records={
            '0': 'allowed', '1': {'user_id': 77, 'revision': 2},
        })
        for update_id, raw in enumerate((
            NavigationCallback(view='current', item=0).pack(),
            AdminCallback(view='current', action='ban', item=1).pack(),
            'adm:malformed',
            'n:stale:0',
        ), 1):
            await dispatcher.feed_update(self.bot, Update(update_id=update_id, callback_query=self.callback(raw)))
        self.assertEqual(hits, ['allowed', 'ban'])


class TestLimiterAndUploadGate(unittest.IsolatedAsyncioTestCase):
    def policy(self, **changes):
        return replace(config.bot,
            updates_per_second=1, updates_burst=2,
            global_updates_per_second=1, global_updates_burst=100,
            throttle_user_capacity=3, throttle_idle_seconds=20,
            throttle_notice_seconds=5, upload_cooldown_seconds=10,
            **changes,
        )

    def test_burst_refill_and_notice_cooldown(self):
        now = [100.0]
        limiter = RequestLimiter(self.policy(), clock=lambda: now[0])
        self.assertEqual(limiter.check(1), (True, False))
        self.assertEqual(limiter.check(1), (True, False))
        self.assertEqual(limiter.check(1), (False, True))
        self.assertEqual(limiter.check(1), (False, False))
        now[0] += 1
        self.assertTrue(limiter.check(1)[0])

    def test_photo_cooldown_does_not_block_navigation(self):
        now = [100.0]
        limiter = RequestLimiter(self.policy(), clock=lambda: now[0])
        self.assertTrue(limiter.check(1, photo=True)[0])
        self.assertFalse(limiter.check(1, photo=True)[0])
        self.assertTrue(limiter.check(1)[0])
        now[0] += 10
        self.assertTrue(limiter.check(1, photo=True)[0])

    def test_global_cap_limits_denials_and_user_cache_stays_bounded(self):
        now = [100.0]
        policy = replace(self.policy(), global_updates_burst=2)
        limiter = RequestLimiter(policy, clock=lambda: now[0])
        self.assertTrue(limiter.check(1)[0])
        self.assertTrue(limiter.check(2)[0])
        self.assertEqual(limiter.check(3), (False, False))
        for actor in range(4, 1000):
            limiter.check(actor)
        self.assertLessEqual(len(limiter.users), policy.throttle_user_capacity)
        now[0] += policy.throttle_idle_seconds + 1
        limiter.check(1001)
        self.assertEqual(len(limiter.users), 1)

    async def test_upload_capacity_and_per_actor_exclusion(self):
        gate = UploadGate(2)
        async with gate.acquire(1):
            with self.assertRaises(UploadBusy):
                async with gate.acquire(1):
                    pass
            async with gate.acquire(2):
                with self.assertRaises(UploadBusy):
                    async with gate.acquire(3):
                        pass
            async with gate.acquire(3):
                self.assertEqual(gate.actors, {1, 3})
        self.assertEqual(gate.actors, set())

    async def test_cancelled_upload_releases_capacity(self):
        gate, entered = UploadGate(1), asyncio.Event()

        async def upload():
            async with gate.acquire(1):
                entered.set()
                await asyncio.Event().wait()

        task = asyncio.create_task(upload())
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        async with gate.acquire(2):
            self.assertEqual(gate.actors, {2})


class TestAuthenticationAndErrors(TelegramFixture):
    def repository(self, *, status=UserStatus.USER, banned=False, actor=5, missing=False):
        principal = User.create(
            telegram_id=actor, telegram_username=None, name='Test',
            status=status, isbanned=banned,
        ).value
        users = {} if missing else {actor: principal}

        async def add(user):
            users.setdefault(user.telegram_id, user)
            return True

        repository = SimpleNamespace(
            get_user_by_id=AsyncMock(side_effect=lambda actor_id: users.get(actor_id)),
            add_user=AsyncMock(side_effect=add), update_user_info=AsyncMock(), users=users,
        )
        return repository

    async def test_registration_accepts_message_without_username(self):
        repository = self.repository(missing=True)
        handler, data = AsyncMock(), {}
        with patch.object(settings, 'telegram_admin_ids', (999,), create=True):
            await AuthenticationMiddleware(repository)(handler, self.message(username=None), data)
        handler.assert_awaited_once()
        self.assertIsNone(data['user'].telegram_username)
        self.assertEqual(data['user'].telegram_id, 5)
        self.assertEqual(len(repository.users), 1)

    async def test_banned_message_and_callback_never_reach_handler(self):
        repository = self.repository(banned=True)
        handler = AsyncMock()
        with patch.object(settings, 'telegram_admin_ids', (999,), create=True):
            middleware = AuthenticationMiddleware(repository)
            await middleware(handler, self.message(), {})
            await middleware(handler, self.callback('adm:anything'), {})
        handler.assert_not_awaited()
        repository.update_user_info.assert_not_awaited()
        self.assertIsInstance(self.session.requests[0], SendMessage)
        self.assertIsInstance(self.session.requests[1], AnswerCallbackQuery)

    async def test_fixed_admin_remains_admin_despite_old_database_role_and_ban(self):
        repository = self.repository(banned=True)
        data = {}
        with patch.object(settings, 'telegram_admin_ids', (5,), create=True):
            await AuthenticationMiddleware(repository)(AsyncMock(), self.callback('adm:anything'), data)
        self.assertTrue(data['user'].is_admin)
        self.assertFalse(data['user'].isbanned)

    async def test_dynamic_role_changes_are_read_for_next_callback_without_restart(self):
        repository = self.repository(status=UserStatus.ADMIN)
        handler, first, second = AsyncMock(), {}, {}
        with patch.object(settings, 'telegram_admin_ids', (999,), create=True):
            middleware = AuthenticationMiddleware(repository)
            await middleware(handler, self.callback('adm:anything'), first)
            repository.users[5] = repository.users[5].model_copy(update={'status': UserStatus.USER})
            await middleware(handler, self.callback('adm:anything'), second)
        self.assertTrue(first['user'].is_admin)
        self.assertFalse(second['user'].is_admin)
        self.assertEqual(repository.get_user_by_id.await_count, 2)

    async def test_group_updates_are_rejected_before_handler(self):
        handler = AsyncMock()
        update = Update(update_id=1, message=self.message(chat_type='group'))
        await AbuseControlMiddleware()(handler, update, {})
        handler.assert_not_awaited()
        self.assertEqual(self.session.requests, [])

    async def test_error_boundary_hides_technical_error_and_propagates_cancellation(self):
        update = Update(update_id=1, message=self.message())
        with patch('src.utils.middlewares.errors.exception_logger') as logger:
            await ErrorBoundaryMiddleware()(AsyncMock(side_effect=RuntimeError('DB_PASSWORD')), update, {})
        logger.exception.assert_called_once()
        self.assertNotIn('DB_PASSWORD', self.session.requests[-1].text)
        before = len(self.session.requests)
        with self.assertRaises(asyncio.CancelledError):
            await ErrorBoundaryMiddleware()(AsyncMock(side_effect=asyncio.CancelledError()), update, {})
        self.assertEqual(len(self.session.requests), before)
