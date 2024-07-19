from aiogram import types
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
from aiogram import Dispatcher
from aiogram.filters import Command
from aiogram.types import Message

async def main_menu(message: Message, is_admin: bool):
    buttons = [
        [KeyboardButton(text='О боте')],
        [KeyboardButton(text='Поддержка')],
        [KeyboardButton(text='Собрать изображение')]
    ]
    if is_admin:
        buttons.append([KeyboardButton(text='Админ панель')])
    
    keyboard = ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)
    await message.answer('Главное меню', reply_markup=keyboard)

def register_handlers_main_menu(dp: Dispatcher):
    dp.message.register(main_menu, Command('start'))
