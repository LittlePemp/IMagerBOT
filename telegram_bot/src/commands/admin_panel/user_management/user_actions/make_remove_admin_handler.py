from aiogram import Dispatcher, F

from settings import settings
from src.models.user import UserStatus
from src.utils.filters.request_filters.admin_filter import AdminFilter
from ...ui import AdminCallback, command, show
from ..user_management_handler import render_user
from ..user_management_states import UserManagementStates


async def make_remove_admin(callback, state, user_repository):
    action = await command(callback, state)
    if action is None:
        return
    try:
        user = await user_repository.change_access(
            callback.from_user.id, action.record['user_id'],
            status=UserStatus.ADMIN if action.action == 'grant' else UserStatus.USER,
            expected_revision=action.record['revision'],
            fixed_admin_ids=settings.telegram_admin_ids,
        )
    except ValueError as error:
        await show(callback, state, str(error), [('Обновить поиск', 'users', 0)])
        return
    await render_user(callback, state, user)


def register_handlers_make_remove_admin(dp: Dispatcher):
    dp.callback_query.register(
        make_remove_admin, AdminCallback.filter(F.action.in_({'grant', 'revoke'})),
        UserManagementStates.waiting_for_action, AdminFilter(),
    )
