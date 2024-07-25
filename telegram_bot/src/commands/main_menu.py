from aiogram import Dispatcher, types
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder

from src.infrastructure.data.models.user import User
from src.utils.loggers import bot_requests_logger


async def main_menu(message: types.Message, user: User):
    bot_requests_logger.info(f"Handling main menu for user: {user.telegram_id}, is_admin: {user.is_admin}")
    builder = InlineKeyboardBuilder()
    builder.button(text='About', callback_data='about')
    builder.button(text='Generate Image', callback_data='generate_image')
    builder.button(text='Support', callback_data='support')
    if user.is_admin:
        builder.button(text='Admin Panel', callback_data='admin_panel')
    builder.adjust(2)
    
    await message.answer('Main Menu', reply_markup=builder.as_markup())
    bot_requests_logger.info("Main menu sent")

def register_handlers_main_menu(dp: Dispatcher):
    dp.message.register(main_menu, Command('start'))
    dp.message.register(main_menu, Command('menu'))
