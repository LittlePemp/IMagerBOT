from aiogram import Dispatcher, F
from aiogram.types import CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder

from src.commands.common_keyboards import add_home_button
from src.utils.navigation import render_view


def support_keyboard():
    builder = InlineKeyboardBuilder()
    add_home_button(builder, text="Главное меню")
    builder.adjust(1)
    return builder.as_markup()


async def show_support(callback: CallbackQuery, state):
    await state.clear()
    await render_view(callback, state,
        "Выберите «Собрать изображение» в главном меню и следуйте подсказкам.\n\n"
        "Отменить текущий ввод можно командой /cancel.",
        support_keyboard(),
    )
    await callback.answer()


def register_handlers_support(dp: Dispatcher):
    dp.callback_query.register(show_support, F.data == "support")
