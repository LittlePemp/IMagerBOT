from aiogram import Dispatcher, F
from aiogram.types import CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder

from src.commands.common_keyboards import add_home_button
from src.utils.navigation import render_view


def support_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="Назад", callback_data="main_menu")
    add_home_button(builder)
    builder.adjust(1)
    return builder.as_markup()


async def show_support(callback: CallbackQuery, state):
    await state.clear()
    await render_view(callback, state,
        "Для нового изображения откройте главное меню. "
        "Если приём задания уточняется, сохраните его номер: повторный расчёт создавать не нужно. "
        "Принятые задания продолжают выполняться после перезапуска бота.",
        support_keyboard(),
    )
    await callback.answer()


def register_handlers_support(dp: Dispatcher):
    dp.callback_query.register(show_support, F.data == "support")
