from aiogram import F
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
from src.commands.common_keyboards import HOME_CALLBACK
from src.utils.navigation import render_view, STALE_VIEW_TEXT

MAIN_MENU_TEXT = 'Главное меню'


def build_main_menu_keyboard(is_admin):
    builder = InlineKeyboardBuilder()
    for title, action in [
        ('Собрать изображение', 'generate_image'),
        ('О проекте', 'about'), ('Поддержка', 'support'),
    ]:
        builder.button(text=title, callback_data=action)
    if is_admin:
        builder.button(text='Админ-панель', callback_data='admin_panel')
    builder.adjust(2)
    return builder.as_markup()


async def main_menu(message, state, user):
    await state.clear()
    await render_view(message, state, MAIN_MENU_TEXT, build_main_menu_keyboard(user.is_admin))


async def main_menu_callback(callback, state, user):
    await state.clear()
    await render_view(callback, state, MAIN_MENU_TEXT, build_main_menu_keyboard(user.is_admin))
    await callback.answer()


async def help_command(message, user):
    commands = '/start, /menu — главное меню\n/cancel — отменить текущий ввод\n/help — справка'
    if user.is_admin:
        commands += '\n/admin — админ-панель'
    await message.answer(commands)


async def unknown_command(message):
    await message.answer('Команда не распознана. Используйте /help или /menu.')


async def unmatched_message(message):
    await message.answer('Используйте текущую клавиатуру или откройте /menu. Для отмены ввода — /cancel.')


async def unmatched_callback(callback):
    await callback.answer(STALE_VIEW_TEXT)


def register_handlers_main_menu(dp):
    dp.message.register(main_menu, Command('start', 'menu', 'cancel'))
    dp.message.register(help_command, Command('help'))
    dp.callback_query.register(main_menu_callback, F.data == HOME_CALLBACK)


def register_fallback(dp):
    dp.message.register(unknown_command, F.text.startswith('/'))
    dp.message.register(unmatched_message)
    dp.callback_query.register(unmatched_callback)
