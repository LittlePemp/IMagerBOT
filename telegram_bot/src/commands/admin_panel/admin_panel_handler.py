from aiogram import Dispatcher
from aiogram.filters import Command
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton

async def admin_panel(message: Message, is_admin: bool):
    if not is_admin:
        await message.answer('У вас нет доступа к этой панели.')
        return

    buttons = [
        [KeyboardButton(text='Управление группами изображений')],
        [KeyboardButton(text='Управление пользователями')],
        [KeyboardButton(text='Настройки')]
    ]
    
    keyboard = ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)
    await message.answer('Админ панель', reply_markup=keyboard)

def register_handlers_admin_panel(dp: Dispatcher):
    dp.message.register(admin_panel, Command('admin'))
