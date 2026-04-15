from aiogram.utils.keyboard import InlineKeyboardBuilder

from src.commands.common_keyboards import add_home_button


def user_action_keyboard(isbanned, is_admin):
    builder = InlineKeyboardBuilder()
    if isbanned:
        builder.button(text="Разблокировать пользователя", callback_data="user_action:unblock_user")
    else:
        builder.button(text="Заблокировать пользователя", callback_data="user_action:block_user")
    if is_admin:
        builder.button(text="Снять администратора", callback_data="user_action:remove_admin")
    else:
        builder.button(text="Назначить администратора", callback_data="user_action:make_admin")
    builder.button(text="Назад", callback_data="admin_panel:users")
    add_home_button(builder)
    builder.adjust(1)
    return builder.as_markup()
