from aiogram import Dispatcher, types, F
from aiogram.types import CallbackQuery
from aiogram.fsm.context import FSMContext
from src.infrastructure.data.repositories.user_repository import UserRepository
from src.commands.admin_panel.user_management.user_management_states import UserManagementStates
from .user_action_keyboard import back_to_status_keyboard
from ..user_management_handler import format_user_info

async def block_unblock_user(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    user_repository: UserRepository = callback.bot.user_repository
    user_id = data['user_id']
    isbanned = data['isbanned']
    new_status = not isbanned

    await user_repository.update_user(user_id, {'isbanned': new_status})
    user = await user_repository.get_user_by_id(user_id)
    user_info = format_user_info(user)
    await callback.message.edit_text(user_info, reply_markup=back_to_status_keyboard())
    await state.update_data(isbanned=new_status)
    await callback.answer()

def register_handlers_block_unblock(dp: Dispatcher):
    dp.callback_query.register(
        block_unblock_user, 
        F.data.in_({'user_action:block_user', 'user_action:unblock_user'}), 
        UserManagementStates.waiting_for_action
    )
