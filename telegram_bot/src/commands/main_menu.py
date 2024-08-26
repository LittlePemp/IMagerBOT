from aiogram import Dispatcher, types
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder, InlineKeyboardMarkup
from src.models.user import User
from src.utils.loggers import bot_requests_logger, exception_logger

from .abstractions import BaseHandler


class MainMenuKeyboard():
    def get_keyboard(self, user: User) -> InlineKeyboardMarkup:
        builder = InlineKeyboardBuilder()
        builder.button(text='О нас', callback_data='about')
        builder.button(text='Сгенерировать изображение', callback_data='generate_image')
        builder.button(text='Поддержка', callback_data='support')
        if user.is_admin:
            builder.button(text='Панель администратора', callback_data='admin_panel')
        builder.adjust(2)
        return builder.as_markup()


class MainMenuHandler(BaseHandler):
    keyboard = MainMenuKeyboard()

    @staticmethod
    async def main_menu(message: types.Message, user: User):
        ''' Handles the main menu display for the user. '''
        try:
            bot_requests_logger.info(f'Handling main menu for user: {user.telegram_id}, is_admin: {user.is_admin}')

            await message.answer('Главное меню', reply_markup=MainMenuHandler.keyboard.get_keyboard(user))

            bot_requests_logger.debug('Main menu sent')

        except Exception as e:
            exception_logger.error(f'Error handling main menu for user: {user.telegram_id}, error: {e}')
            await message.answer('Произошла ошибка при отображении главного меню.')

    @staticmethod
    def register_handlers(dp: Dispatcher):
        dp.message.register(MainMenuHandler.main_menu, Command('start'))
        dp.message.register(MainMenuHandler.main_menu, Command('menu'))
