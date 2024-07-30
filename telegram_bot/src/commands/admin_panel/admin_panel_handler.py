from aiogram import Dispatcher, types, F
from aiogram.filters import Command
from src.utils.filters.request_filters.admin_filter import AdminFilter
from src.utils.loggers import bot_requests_logger

from .admin_panel_keyboard import admin_panel_keyboard


async def admin_panel(message: types.Message):
    bot_requests_logger.info('Admin panel command triggered')
    await message.answer('Admin Panel', reply_markup=admin_panel_keyboard())

async def admin_panel_callback(callback_query: types.CallbackQuery):
    bot_requests_logger.info(f'Admin panel callback triggered by user: {callback_query.from_user.id}')
    await callback_query.message.answer('Admin Panel', reply_markup=admin_panel_keyboard())
    await callback_query.answer()

def register_handlers_admin_panel(dp: Dispatcher):
    dp.message.register(admin_panel, Command('admin'), AdminFilter())
    dp.callback_query.register(admin_panel_callback, F.data == 'admin_panel', AdminFilter())
