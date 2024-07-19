import asyncio
import logging
from aiogram import Bot, Dispatcher
from motor.motor_asyncio import AsyncIOMotorClient
from settings import settings
from src.utils.middlewares import logging_middleware
from src.utils.middlewares.admin_middleware import AdminMiddleware
from src.infrastructure.data.repositories.user_repository import UserRepository
from src.commands.main_menu import register_handlers_main_menu
from src.commands.admin_panel.admin_panel_handler import register_handlers_admin_panel

logging.basicConfig(level=logging.INFO)

bot = Bot(token=settings.api_token)
dp = Dispatcher()

mongo_client = AsyncIOMotorClient(settings.mongodb_uri)
db = mongo_client['telegram']

user_repository = UserRepository(db)

dp.update.outer_middleware(logging_middleware)
dp.update.outer_middleware(AdminMiddleware(user_repository))

register_handlers_main_menu(dp)
register_handlers_admin_panel(dp)

async def main():
    await dp.start_polling(bot)

if __name__ == '__main__':
    asyncio.run(main())
