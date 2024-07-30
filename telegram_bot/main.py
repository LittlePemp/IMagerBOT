import asyncio
import logging
from datetime import datetime

from aiogram import Bot, Dispatcher
from motor.motor_asyncio import AsyncIOMotorClient
from settings import settings
from src.commands.admin_panel.admin_panel_handler import \
    register_handlers_admin_panel
from src.commands.admin_panel.user_management.user_management_handler import \
    register_handlers_user_management
from src.commands.main_menu import register_handlers_main_menu
from src.infrastructure.data.models.user import User, UserStatus
from src.infrastructure.data.unit_of_work import get_uow
from src.utils.middlewares import logging_middleware
from src.utils.middlewares.authentication_middleware import \
    AuthenticationMiddleware

from src.utils.loggers import exception_logger

logging.basicConfig(level=logging.INFO)

bot = Bot(token=settings.api_token.get_secret_value())
dp = Dispatcher()

# DB
mongo_client = AsyncIOMotorClient(settings.mongodb_uri)
db = mongo_client.get_database(settings.database_name)
uow = get_uow(db)

# Middlewares
dp.update.outer_middleware(logging_middleware)
dp.message.middleware(AuthenticationMiddleware(uow.user_repository))

# Filters

# Register handlers
register_handlers_main_menu(dp)
register_handlers_admin_panel(dp)
# register_handlers_user_management(dp, uow.user_repository)

async def set_initial_admin():
    async with uow as uow_instance:
        admins = await uow_instance.user_repository.get_admins()
        if not admins:
            exception_logger.error('Failed to fetch admins')
            return

        settings.telegram_admin_ids.extend(
            admin.telegram_id for admin in admins if admin.telegram_id not in settings.telegram_admin_ids
        )
        for admin_id in settings.telegram_admin_ids:
            user = await uow_instance.user_repository.get_user_by_id(admin_id)
            if user:
                await uow_instance.user_repository.update_user(admin_id, {'status': UserStatus.ADMIN})
            else:
                user_data = User(
                    telegram_id=admin_id,
                    telegram_username='-',
                    name='Admin',
                    registered_datetime_utc=datetime.now(settings.tzinfo),
                    last_activity_datetime_utc=datetime.now(settings.tzinfo),
                    isbanned=False,
                    status=UserStatus.ADMIN
                )
                await uow_instance.user_repository.add_user(user_data)

async def main():
    settings.create_directories()  # TODO:
    await set_initial_admin()
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == '__main__':
    asyncio.run(main())
