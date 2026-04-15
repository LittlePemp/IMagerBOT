from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder


HOME_CALLBACK = "main_menu"


def add_home_button(builder: InlineKeyboardBuilder, text: str = "Домой") -> InlineKeyboardBuilder:
    builder.button(text=text, callback_data=HOME_CALLBACK)
    return builder


def home_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    add_home_button(builder)
    builder.adjust(1)
    return builder.as_markup()
