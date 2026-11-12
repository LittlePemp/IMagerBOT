from aiogram import Dispatcher, F
from aiogram.fsm.context import FSMContext

from settings import settings
from src.utils.filters.request_filters.admin_filter import AdminFilter
from ..ui import AdminCallback, command, show
from .user_management_states import UserManagementStates


def format_user_info(user):
    fixed = user.telegram_id in settings.telegram_admin_ids
    return (
        f"Username: {user.telegram_username or 'не указан'}\n"
        f"ID: {user.telegram_id}\n"
        f"Зарегистрирован: {user.registered_datetime_utc:%d.%m.%Y}\n"
        f"Заблокирован: {'да' if user.isbanned else 'нет'}\n"
        f"Роль: {'постоянный администратор' if fixed else user.status.value}"
    )


async def render_user(event, state, user):
    fixed = user.telegram_id in settings.telegram_admin_ids
    buttons = []
    if not fixed:
        buttons.extend([
            ('Разблокировать' if user.isbanned else 'Заблокировать', 'unban' if user.isbanned else 'ban', 0),
            ('Снять администратора' if user.is_admin else 'Назначить администратора', 'revoke' if user.is_admin else 'grant', 0),
        ])
    buttons.append(('Найти другого пользователя', 'users', 0))
    await show(
        event, state, format_user_info(user), buttons,
        records={'0': {'user_id': user.telegram_id, 'revision': user.revision}},
        fsm=UserManagementStates.waiting_for_action,
    )


async def user_management(callback, state: FSMContext, user_repository):
    if not await command(callback, state):
        return
    count = await user_repository.get_user_count()
    await show(callback, state, f'Пользователей: {count}.\nВведите ID или @username:', [
        ('Назад', 'home', 0),
    ], fsm=UserManagementStates.waiting_for_username_or_id)


async def receive_username_or_id(message, state: FSMContext, user_repository):
    text = (message.text or '').strip()
    user = await fetch_user(text, user_repository)
    if user:
        await render_user(message, state, user)
    else:
        await show(message, state, 'Пользователь не найден. Введите ID или @username:', [
            ('Назад', 'home', 0),
        ], fsm=UserManagementStates.waiting_for_username_or_id)


async def fetch_user(text, user_repository):
    if text.isascii() and text.isdigit():
        return await user_repository.get_user_by_id(int(text))
    return await user_repository.get_user_by_username(text.removeprefix('@').casefold())


def register_handlers_user_management(dp: Dispatcher):
    dp.callback_query.register(user_management, AdminCallback.filter(F.action == 'users'), AdminFilter())
    dp.message.register(receive_username_or_id, UserManagementStates.waiting_for_username_or_id, AdminFilter())
