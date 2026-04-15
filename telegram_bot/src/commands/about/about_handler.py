from aiogram import Dispatcher, F
from aiogram.types import CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder

from src.commands.common_keyboards import add_home_button


def about_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="Назад", callback_data="main_menu")
    add_home_button(builder)
    builder.adjust(1)
    return builder.as_markup()


async def show_about(callback: CallbackQuery):
    await callback.message.edit_text(
        "IMager BOT собирает изображения из набора маленьких картинок и формирует мозаику.",
        reply_markup=about_keyboard(),
    )
    await callback.answer()


def register_handlers_about(dp: Dispatcher):
    dp.callback_query.register(show_about, F.data == "about")
