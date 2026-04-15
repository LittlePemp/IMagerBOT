from aiogram.utils.keyboard import InlineKeyboardBuilder

from src.commands.common_keyboards import add_home_button


def back_to_status_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="Назад", callback_data="user_action:back_to_status")
    add_home_button(builder)
    builder.adjust(1)
    return builder.as_markup()
