from aiogram import Dispatcher, types, F
from aiogram.types import CallbackQuery, Message
from aiogram.fsm.context import FSMContext
from src.infrastructure.data.repositories.user_repository import UserRepository
from src.utils.loggers import bot_requests_logger
from src.commands.admin_panel.user_management.user_management_states import UserManagementStates
from src.commands.admin_panel.user_management.user_management_keyboard import user_action_keyboard
from src.models.user import User, UserStatus


async def user_management(callback: CallbackQuery, state: FSMContext):
    user_repository: UserRepository = callback.bot.user_repository
    user_count = await user_repository.get_user_count()
    await callback.message.edit_text(f'Общее количество пользователей: {user_count}\nПожалуйста, введите username или ID пользователя:')
    await state.set_state(UserManagementStates.waiting_for_username_or_id)
    await callback.answer()

async def receive_username_or_id(message: Message, state: FSMContext):
    user_repository: UserRepository = message.bot.user_repository
    user_input = message.text

    user = await fetch_user(user_input, user_repository)

    if not user:
        await message.answer('Пользователь не найден.')
        await state.clear()

    user_info = format_user_info(user)
    await state.update_data(user_id=user.telegram_id, isbanned=user.isbanned, is_admin=(user.status == UserStatus.ADMIN))
    await message.answer(user_info, reply_markup=user_action_keyboard(user.isbanned, user.status == UserStatus.ADMIN))
    await state.set_state(UserManagementStates.waiting_for_action)

async def back_to_status(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    user_repository: UserRepository = callback.bot.user_repository
    user_id = data['user_id']
    user = await user_repository.get_user_by_id(user_id)
    user_info = format_user_info(user)
    await callback.message.edit_text(user_info, reply_markup=user_action_keyboard(user.isbanned, user.status == UserStatus.ADMIN))
    await callback.answer()

def format_user_info(user: User) -> str:
    return (
        f'Имя пользователя: {user.telegram_username}\n'
        f'ID пользователя: {user.telegram_id}\n'
        f'Зарегистрирован: {user.registered_datetime_utc}\n'
        f'Последняя активность: {user.last_activity_datetime_utc}\n'
        f'Заблокирован: {user.isbanned}\n'
        f'Статус: {user.status.value}'
    )

async def fetch_user(user_input: str, user_repository: UserRepository):
    if user_input.isdigit():
        return await user_repository.get_user_by_id(int(user_input))
    else:
        user = await user_repository.get_user_by_username(user_input)
        if user:
            await user_repository.update_user(user.telegram_id, {'telegram_username': user_input})
        return user

def register_handlers_user_management(dp: Dispatcher):
    dp.callback_query.register(user_management, F.data == 'admin_panel:users')
    dp.message.register(receive_username_or_id, UserManagementStates.waiting_for_username_or_id)
    dp.callback_query.register(back_to_status, F.data == 'user_action:back_to_status')
