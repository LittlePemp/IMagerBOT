from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram import Dispatcher
from aiogram.fsm.storage.memory import SimpleEventIsolation
from aiogram.methods import EditMessageText, SendMessage
from aiogram.types import Update

from src.commands.main_menu import register_handlers_main_menu, register_fallback
from src.commands.support.support_handler import register_handlers_support
from src.utils.navigation import NavigationMiddleware, STALE_VIEW_TEXT, bind_view
from test_navigation_security import TelegramFixture


class UserMenuTests(TelegramFixture):
    def dispatcher(self):
        self.gateway = SimpleNamespace(get_job=AsyncMock(), retry_delivery=AsyncMock())
        dp = Dispatcher(storage=self.storage, events_isolation=SimpleEventIsolation())
        dp['user'] = SimpleNamespace(telegram_id=5, is_admin=False)
        dp['generation_service'] = SimpleNamespace(gateway=self.gateway)
        dp.callback_query.outer_middleware(NavigationMiddleware())
        register_handlers_main_menu(dp)
        register_handlers_support(dp)
        register_fallback(dp)
        return dp

    def last_view(self):
        return next(item for item in reversed(self.session.requests) if isinstance(item, (SendMessage, EditMessageText)))

    async def open_menu(self, dp):
        message = self.message().model_copy(update={'text': '/menu'})
        await dp.feed_update(self.bot, Update(update_id=1, message=message))
        return self.last_view()

    async def test_menu_has_no_separate_image_status_or_delivery_controls(self):
        dp = self.dispatcher()
        menu = await self.open_menu(dp)
        titles = [button.text for row in menu.reply_markup.inline_keyboard for button in row]
        self.assertEqual(titles, ['Собрать изображение', 'О проекте', 'Поддержка'])

    async def test_support_has_plain_instructions_and_only_home_navigation(self):
        dp = self.dispatcher()
        menu = await self.open_menu(dp)
        support = next(button for row in menu.reply_markup.inline_keyboard for button in row if button.text == 'Поддержка')
        message_id = self.session.next_message
        await dp.feed_update(self.bot, Update(update_id=2, callback_query=self.callback(support.callback_data, message_id=message_id)))
        view = self.last_view()
        for hidden in ('задани', 'номер', 'ядр', 'UUID', 'private-internal-id'):
            self.assertNotIn(hidden, view.text)
        self.assertNotIn('повтор', view.text.lower())
        self.assertNotIn('Моё изображение', view.text)
        titles = [button.text for row in view.reply_markup.inline_keyboard for button in row]
        self.assertEqual(titles, ['Главное меню'])
        home = view.reply_markup.inline_keyboard[0][0]
        await dp.feed_update(self.bot, Update(update_id=3, callback_query=self.callback(home.callback_data, message_id=message_id)))
        self.assertEqual(self.last_view().text, 'Главное меню')

    async def test_retired_callbacks_cannot_show_status_or_restart_delivery(self):
        dp = self.dispatcher()
        for index, action in enumerate(('my_image', 'job_status:private-id', 'job_retry:private-id'), 1):
            with self.subTest(action=action):
                # A saved screen from the previous version may still have a valid
                # navigation record. Its removed action must reach only fallback.
                await bind_view(self.state, self.message(), owner_id=5,
                                token='previous-view', records={'0': action})
                await dp.feed_update(self.bot, Update(update_id=index,
                    callback_query=self.callback('n:previous-view:0')))
                self.assertEqual(self.session.requests[-1].text, STALE_VIEW_TEXT)
        self.gateway.get_job.assert_not_awaited()
        self.gateway.retry_delivery.assert_not_awaited()

    async def test_retired_commands_are_unrecognized_and_help_does_not_advertise_them(self):
        dp = self.dispatcher()
        for index, command in enumerate(('/status', '/retry_delivery private-id'), 1):
            with self.subTest(command=command):
                message = self.message().model_copy(update={'text': command})
                await dp.feed_update(self.bot, Update(update_id=index, message=message))
                self.assertEqual(self.last_view().text, 'Команда не распознана. Используйте /help или /menu.')
        message = self.message().model_copy(update={'text': '/help'})
        await dp.feed_update(self.bot, Update(update_id=3, message=message))
        self.assertNotIn('/status', self.last_view().text)
        self.assertNotIn('/retry_delivery', self.last_view().text)
        self.gateway.get_job.assert_not_awaited()
        self.gateway.retry_delivery.assert_not_awaited()
