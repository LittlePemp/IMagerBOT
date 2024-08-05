from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

def user_action_keyboard(isbanned: bool, is_admin: bool):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text='Разблокировать' if isbanned else 'Заблокировать', callback_data='user_action:unblock_user' if isbanned else 'user_action:block_user'),
            InlineKeyboardButton(text='Снять администратора' if is_admin else 'Назначить администратора', callback_data='user_action:remove_admin' if is_admin else 'user_action:make_admin')
        ]
    ])
    return keyboard

def back_to_status_keyboard():
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text='Назад', callback_data='user_action:back_to_status')]
    ])
    return keyboard
