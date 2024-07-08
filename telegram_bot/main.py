from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.contrib.middlewares.logging import LoggingMiddleware
from aiogram.utils import executor
from handlers import register_handlers
from telegram_bot.src.settings import settings
from utils.loggers import bot_requests_logger

bot = Bot(token=settings.telegram_token)
storage = MemoryStorage()
dp = Dispatcher(bot, storage=storage)
dp.middleware.setup(LoggingMiddleware(logger=bot_requests_logger))

register_handlers(dp)

if __name__ == "__main__":
    executor.start_polling(dp, skip_updates=True)
