from aiogram import Dispatcher, F
from aiogram.types import CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder

from src.commands.common_keyboards import add_home_button
from src.utils.navigation import render_view


def support_keyboard(support_bot_url=None):
    builder = InlineKeyboardBuilder()
    if support_bot_url:
        builder.button(text='Написать в поддержку', url=support_bot_url)
    add_home_button(builder, text="Главное меню")
    builder.adjust(1)
    return builder.as_markup()


async def show_support(callback: CallbackQuery, state, support_bot_url=None):
    await state.clear()
    text = ('Опишите проблему в боте поддержки. Можно приложить фото или файл.'
            if support_bot_url else
            'Выберите «Собрать изображение» в главном меню и следуйте подсказкам.\n\n'
            'Отменить текущий ввод можно командой /cancel.')
    await render_view(callback, state,
        text, support_keyboard(support_bot_url),
    )
    await callback.answer()


def register_handlers_support(dp: Dispatcher):
    dp.callback_query.register(show_support, F.data == "support")
