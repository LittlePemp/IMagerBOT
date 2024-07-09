from aiogram import types
from src.loader import dp
from src.utils.loggers import bot_requests_logger


@dp.message_handler(commands=['start'])
async def start_command(message: types.Message):
    bot_requests_logger.info(f"Received /start command from {message.from_user.id}")
    await message.answer("HI HI.")
