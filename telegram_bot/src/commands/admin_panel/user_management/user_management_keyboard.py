from aiogram.utils.keyboard import InlineKeyboardBuilder

def user_action_keyboard(isbanned, is_admin):
    builder = InlineKeyboardBuilder()
    if isbanned:
        builder.button(text='Разблокировать пользователя', callback_data='user_action:unblock_user')
    else:
        builder.button(text='Заблокировать пользователя', callback_data='user_action:block_user')
    if is_admin:
        builder.button(text='Снять администратора', callback_data='user_action:remove_admin')
    else:
        builder.button(text='Назначить администратора', callback_data='user_action:make_admin')
    builder.adjust(1)
    return builder.as_markup()
