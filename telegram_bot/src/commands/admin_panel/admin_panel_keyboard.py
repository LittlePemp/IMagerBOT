from aiogram.utils.keyboard import InlineKeyboardBuilder

from src.commands.common_keyboards import add_home_button


def admin_panel_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="Пользователи", callback_data="admin_panel:users")
    builder.button(text="Параметры сборки изображения", callback_data="admin_panel:image_params")
    add_home_button(builder)
    builder.adjust(1)
    return builder.as_markup()
