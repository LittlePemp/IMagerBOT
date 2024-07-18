import asyncio
import logging
from aiogram import Bot, Dispatcher, types
from aiogram.filters.command import Command
from motor.motor_asyncio import AsyncIOMotorClient
from settings import settings
from src.utils.middlewares import logging_middleware
from src.infrastructure.data.repositories.user_repository import UserRepository
from src.infrastructure.data.repositories.image_group_repository import ImageGroupRepository
from src.infrastructure.data.repositories.generated_image_repository import GeneratedImageRepository

logging.basicConfig(level=logging.INFO)

bot = Bot(token=settings.api_token)
dp = Dispatcher()

dp.update.outer_middleware(logging_middleware)

mongo_client = AsyncIOMotorClient(settings.mongodb_uri)
db = mongo_client.get_default_database()

user_repository = UserRepository(db)
image_group_repository = ImageGroupRepository(db)
generated_image_repository = GeneratedImageRepository(db)

@dp.message(Command('start'))
async def cmd_start(message: types.Message):
    await message.answer('Hello!')

async def main():
    await dp.start_polling(bot)

if __name__ == '__main__':
    asyncio.run(main())
