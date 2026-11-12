from aiogram import Dispatcher, F, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext

from src.utils.filters.request_filters.admin_filter import AdminFilter
from .ui import AdminCallback, command, show


async def show_admin_menu(event, state):
    await state.clear()
    await show(event, state, 'Админ-панель', [
        ('Пользователи', 'users', 0),
        ('Параметры сборки изображения', 'params', 0),
    ])


async def admin_panel(message: types.Message, state: FSMContext):
    await show_admin_menu(message, state)


async def admin_panel_callback(callback_query: types.CallbackQuery, state: FSMContext):
    if callback_query.data.startswith('adm:') and not await command(callback_query, state):
        return
    if not callback_query.data.startswith('adm:'):
        await callback_query.answer()
    await show_admin_menu(callback_query, state)


def register_handlers_admin_panel(dp: Dispatcher):
    dp.message.register(admin_panel, Command('admin'), AdminFilter())
    dp.callback_query.register(admin_panel_callback, F.data == 'admin_panel', AdminFilter())
    dp.callback_query.register(admin_panel_callback, AdminCallback.filter(F.action == 'home'), AdminFilter())
