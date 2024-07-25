from aiogram.types import ReplyKeyboardMarkup, KeyboardButton

def get_admin_panel_keyboard():
    buttons = [
        [KeyboardButton(text='Управление группами изображений')],
        [KeyboardButton(text='Управление пользователями')],
        [KeyboardButton(text='Настройки')],
        [KeyboardButton(text='В меню')]
    ]
    
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)
