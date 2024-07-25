from aiogram import Dispatcher
from aiogram.filters import Command
from aiogram.types import Message
from src.utils.filters.admin_filter import AdminFilter

from .admin_panel_keyboard import get_admin_panel_keyboard


async def admin_panel(message: Message, user):
    keyboard = get_admin_panel_keyboard()
    await message.answer('Админ панель', reply_markup=keyboard)

def register_handlers_admin_panel(dp: Dispatcher):
    dp.message.register(admin_panel, Command('admin'), AdminFilter())
    dp.message.register(admin_panel, lambda message, data: message.text == 'Админ панель', AdminFilter())
