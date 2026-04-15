from aiogram import Dispatcher, F, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.utils.keyboard import InlineKeyboardBuilder

from src.commands.common_keyboards import HOME_CALLBACK
from src.models.user import User
from src.utils.loggers import bot_requests_logger


MAIN_MENU_TEXT = "Главное меню"


def build_main_menu_keyboard(is_admin: bool):
    builder = InlineKeyboardBuilder()
    builder.button(text="О проекте", callback_data="about")
    builder.button(text="Собрать изображение", callback_data="generate_image")
    builder.button(text="Поддержка", callback_data="support")
    if is_admin:
        builder.button(text="Админ-панель", callback_data="admin_panel")
    builder.adjust(2)
    return builder.as_markup()


async def main_menu(message: types.Message, user: User):
    bot_requests_logger.info(
        f"Handling main menu for user: {user.telegram_id}, is_admin: {user.is_admin}"
    )
    await message.answer(MAIN_MENU_TEXT, reply_markup=build_main_menu_keyboard(user.is_admin))
    bot_requests_logger.info("Main menu sent")


async def main_menu_callback(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    user_repository = callback.bot.user_repository
    user = await user_repository.get_user_by_id(callback.from_user.id)
    if user is None:
        await callback.answer("Пользователь не найден.", show_alert=True)
        return

    await callback.message.edit_text(
        MAIN_MENU_TEXT,
        reply_markup=build_main_menu_keyboard(user.is_admin),
    )
    await callback.answer()


async def help_command(message: types.Message, user: User):
    command_lines = [
        "Доступные команды:",
        "/start - открыть главное меню",
        "/menu - открыть главное меню",
        "/help - показать эту справку",
    ]
    if user.is_admin:
        command_lines.append("/admin - открыть админ-панель")
    command_lines.append("")
    command_lines.append("Основные действия также доступны через кнопки в меню.")

    await message.answer(
        "\n".join(command_lines),
        reply_markup=build_main_menu_keyboard(user.is_admin),
    )


async def unknown_command(message: types.Message, user: User):
    await message.answer(
        "Команда не распознана. Используйте /start, /menu, /help или кнопки в меню.",
        reply_markup=build_main_menu_keyboard(user.is_admin),
    )


def register_handlers_main_menu(dp: Dispatcher):
    dp.message.register(main_menu, Command("start"))
    dp.message.register(main_menu, Command("menu"))
    dp.message.register(help_command, Command("help"))
    dp.message.register(unknown_command, F.text.startswith("/"))
    dp.callback_query.register(main_menu_callback, F.data == HOME_CALLBACK)
