from aiogram import Dispatcher, F, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from src.utils.filters.request_filters.admin_filter import AdminFilter
from src.utils.loggers import bot_requests_logger

from .admin_panel_keyboard import admin_panel_keyboard
from .admin_panel_states import AdminPanelStates
from .user_management.user_management_handler import \
    register_handlers_user_management


async def admin_panel(message: types.Message):
    ''' Handles the /admin command. '''
    try:
        bot_requests_logger.info(f'Admin panel command triggered by user: {message.from_user.id}')
        await message.answer('Панель администратора', reply_markup=admin_panel_keyboard())
    except Exception as e:
        bot_requests_logger.error(f'Error in admin_panel command: {e}')
        await message.answer('Ошибка при открытии панели администратора.')

async def admin_panel_callback(callback_query: types.CallbackQuery, state: FSMContext):
    ''' Handles admin panel button callbacks. '''
    try:
        bot_requests_logger.info(f'Admin panel callback triggered by user: {callback_query.from_user.id}')
        await callback_query.message.answer('Панель администратора', reply_markup=admin_panel_keyboard())
        await callback_query.answer()

        # Set FSM state based on user selection
        if callback_query.data == 'admin_panel:users':
            await state.set_state(AdminPanelStates.managing_users)
        elif callback_query.data == 'admin_panel:image_params':
            await state.set_state(AdminPanelStates.managing_image_groups)
    except Exception as e:
        bot_requests_logger.error(f'Error in admin_panel_callback: {e}')
        await callback_query.message.answer('Ошибка при обработке команды панели администратора.')
        await callback_query.answer()

def register_handlers_admin_panel(dp: Dispatcher):
    register_handlers_user_management(dp)
    dp.message.register(admin_panel, Command('admin'), AdminFilter())
    dp.callback_query.register(admin_panel_callback, F.data.startswith('admin_panel'), AdminFilter())
