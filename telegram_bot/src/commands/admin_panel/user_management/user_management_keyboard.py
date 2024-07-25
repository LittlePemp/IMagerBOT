from aiogram.types import ReplyKeyboardMarkup, KeyboardButton

def get_user_management_keyboard():
    buttons = [
        [KeyboardButton(text='Назначить админа')],
        [KeyboardButton(text='Удалить пользователя')],
        [KeyboardButton(text='В меню')]
    ]
    
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)
