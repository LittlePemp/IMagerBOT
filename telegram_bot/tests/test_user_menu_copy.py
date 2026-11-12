from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram import Dispatcher
from aiogram.fsm.storage.memory import SimpleEventIsolation
from aiogram.methods import EditMessageText, SendMessage
from aiogram.types import Update

from src.commands import jobs
from src.commands.main_menu import register_handlers_main_menu, register_fallback
from src.commands.support.support_handler import register_handlers_support
from src.models.admission import AdmissionReceipt
from src.utils.navigation import NavigationMiddleware, STALE_VIEW_TEXT
from test_navigation_security import TelegramFixture


class UserImageMenuTests(TelegramFixture):
    def dispatcher(self):
        receipt = AdmissionReceipt('private-internal-id', 'pending', '2026-11-12', 'hash', actor_id=5)
        self.admissions = SimpleNamespace(latest=AsyncMock(return_value=receipt), find=AsyncMock(return_value=receipt))
        self.gateway = SimpleNamespace(get_job=AsyncMock(return_value=None), latest_user_job=AsyncMock(return_value=None))
        dp = Dispatcher(storage=self.storage, events_isolation=SimpleEventIsolation())
        dp['user'] = SimpleNamespace(telegram_id=5, is_admin=False)
        dp['generation_service'] = SimpleNamespace(admissions=self.admissions, gateway=self.gateway)
        dp.callback_query.outer_middleware(NavigationMiddleware())
        register_handlers_main_menu(dp)
        jobs.register_handlers_jobs(dp)
        register_handlers_support(dp)
        register_fallback(dp)
        return dp

    def last_view(self):
        return next(item for item in reversed(self.session.requests) if isinstance(item, (SendMessage, EditMessageText)))

    async def open_menu(self, dp):
        message = self.message().model_copy(update={'text': '/menu'})
        await dp.feed_update(self.bot, Update(update_id=1, message=message))
        return self.last_view()

    async def test_menu_opens_latest_image_without_typing_identifier_and_rejects_old_button(self):
        dp = self.dispatcher()
        menu = await self.open_menu(dp)
        button = next(button for row in menu.reply_markup.inline_keyboard for button in row if button.text == 'Моё изображение')
        message_id = self.session.next_message
        await dp.feed_update(self.bot, Update(update_id=2, callback_query=self.callback(button.callback_data, message_id=message_id)))
        view = self.last_view()
        self.assertEqual(view.text, jobs.PENDING_IMAGE_TEXT)
        self.assertNotIn('private-internal-id', view.text)
        self.admissions.latest.assert_awaited_once_with(5)
        await dp.feed_update(self.bot, Update(update_id=3, callback_query=self.callback(button.callback_data, message_id=message_id)))
        self.assertEqual(self.session.requests[-1].text, STALE_VIEW_TEXT)
        self.admissions.latest.assert_awaited_once_with(5)

    async def test_support_is_plain_help_with_a_working_image_status_button(self):
        dp = self.dispatcher()
        menu = await self.open_menu(dp)
        support = next(button for row in menu.reply_markup.inline_keyboard for button in row if button.text == 'Поддержка')
        message_id = self.session.next_message
        await dp.feed_update(self.bot, Update(update_id=2, callback_query=self.callback(support.callback_data, message_id=message_id)))
        view = self.last_view()
        for hidden in ('задани', 'номер', 'ядр', 'UUID', 'private-internal-id'):
            self.assertNotIn(hidden, view.text)
        titles = [button.text for row in view.reply_markup.inline_keyboard for button in row]
        self.assertEqual(titles, ['Моё изображение', 'Главное меню'])
        status = next(button for row in view.reply_markup.inline_keyboard for button in row if button.text == 'Моё изображение')
        await dp.feed_update(self.bot, Update(update_id=3, callback_query=self.callback(status.callback_data, message_id=message_id)))
        self.assertEqual(self.last_view().text, jobs.PENDING_IMAGE_TEXT)
