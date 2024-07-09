from aiogram import Bot, Dispatcher
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from src.settings import settings

bot = Bot(token=settings.telegram_token)
dp = Dispatcher(bot, storage=MemoryStorage())
