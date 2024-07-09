from aiogram.utils import executor
from src.loader import dp
from src.utils.loggers import bot_requests_logger


async def on_startup(dp):
    bot_requests_logger.info("Bot is starting...")

if __name__ == '__main__':
    from src.handlers import start
    executor.start_polling(dp, on_startup=on_startup)
