from aiogram.utils.keyboard import InlineKeyboardBuilder

def admin_panel_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text='Пользователи', callback_data='admin_panel:users')
    builder.button(text='Параметры сборки изображения', callback_data='admin_panel:image_params')
    builder.adjust(1)
    return builder.as_markup()
