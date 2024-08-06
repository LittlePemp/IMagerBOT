from aiogram import Dispatcher, types, F
from aiogram.types import CallbackQuery
from aiogram.fsm.context import FSMContext
from src.infrastructure.data.repositories.user_repository import UserRepository
from src.commands.admin_panel.user_management.user_management_states import UserManagementStates
from src.models.user import UserStatus
from ..user_management_handler import format_user_info
from .user_action_keyboard import back_to_status_keyboard

async def make_remove_admin(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    user_repository: UserRepository = callback.bot.user_repository
    user_id = data['user_id']
    is_admin = data['is_admin']
    new_status = UserStatus.ADMIN if not is_admin else UserStatus.USER

    await user_repository.update_user(user_id, {'status': new_status})
    user = await user_repository.get_user_by_id(user_id)
    user_info = format_user_info(user)
    await callback.message.edit_text(user_info, reply_markup=back_to_status_keyboard())
    await state.update_data(is_admin=(new_status == UserStatus.ADMIN))
    await callback.answer()

def register_handlers_make_remove_admin(dp: Dispatcher):
    dp.callback_query.register(
        make_remove_admin, 
        F.data.in_({'user_action:make_admin', 'user_action:remove_admin'}), 
        UserManagementStates.waiting_for_action
    )
