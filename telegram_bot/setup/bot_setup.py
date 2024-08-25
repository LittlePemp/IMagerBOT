from aiogram import Bot, Dispatcher
from settings import settings


def setup_bot_and_dispatcher():
    bot = Bot(token=settings.api_token.get_secret_value())
    dp = Dispatcher()
    return bot, dp
