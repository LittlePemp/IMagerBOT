from aiogram import Dispatcher, F
from aiogram.types import CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder

from src.commands.common_keyboards import add_home_button


def support_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="Назад", callback_data="main_menu")
    add_home_button(builder)
    builder.adjust(1)
    return builder.as_markup()


async def show_support(callback: CallbackQuery):
    await callback.message.edit_text(
        "Если что-то пошло не так, вернитесь в меню и повторите сценарий. "
        "Для настройки параметров используйте админ-панель.",
        reply_markup=support_keyboard(),
    )
    await callback.answer()


def register_handlers_support(dp: Dispatcher):
    dp.callback_query.register(show_support, F.data == "support")
