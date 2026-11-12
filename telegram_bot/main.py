import asyncio
import logging
import sys
from contextlib import suppress
from datetime import datetime
from pathlib import Path

# Keep the repository's shared packages available when launched locally as
# `cd telegram_bot; python main.py`, with the bot's own imports taking priority.
_REPOSITORY_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPOSITORY_ROOT not in sys.path:
    sys.path.insert(1, _REPOSITORY_ROOT)

from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from motor.motor_asyncio import AsyncIOMotorClient
from imager_config.loader import config
from settings import settings
from src.commands.about.about_handler import register_handlers_about
from src.commands.admin_panel.admin_panel_handler import \
    register_handlers_admin_panel
from src.commands.admin_panel.user_management import \
    register_handlers_user_management_total
from src.commands.main_menu import register_handlers_main_menu
from src.commands.support.support_handler import register_handlers_support
from src.models.user import User, UserStatus
from src.infrastructure.data.unit_of_work import get_uow
from src.utils.middlewares import logging_middleware
from src.utils.middlewares.authentication_middleware import \
    AuthenticationMiddleware

from src.utils.loggers import exception_logger
from src.utils.loggers import bot_requests_logger
from src.commands.admin_panel.image_build_params.image_build_params_handler import register_handlers_image_build_params
from src.commands.image_generation.image_generation_handler import register_handlers_image_generation
from src.infrastructure.services.imager_service import imager_service
from src.infrastructure.services.imager_service.notifier import GenerationNotifier

logging.basicConfig(level=logging.INFO)

bot = Bot(
    token=settings.api_token.get_secret_value(),
    session=AiohttpSession(timeout=config.bot.api_timeout_seconds),
)
dp = Dispatcher()

# DB
mongo_client = AsyncIOMotorClient(
    settings.mongodb_uri, tz_aware=True,
    serverSelectionTimeoutMS=config.mongo.server_selection_timeout_ms,
    connectTimeoutMS=config.mongo.connect_timeout_ms,
    socketTimeoutMS=config.mongo.socket_timeout_ms,
    waitQueueTimeoutMS=config.mongo.wait_queue_timeout_ms,
)
db = mongo_client.get_database(settings.database_name)
uow = get_uow(db)

# bot context
setattr(bot, 'user_repository', uow.user_repository)

# Middlewares
dp.update.outer_middleware(logging_middleware)
dp.message.middleware(AuthenticationMiddleware(uow.user_repository))

# Filters

# Register handlers
register_handlers_main_menu(dp)
register_handlers_about(dp)
register_handlers_support(dp)
register_handlers_admin_panel(dp)
register_handlers_user_management_total(dp)
register_handlers_image_build_params(dp, uow)
register_handlers_image_generation(dp, uow)

async def set_initial_admin():
    async with uow as uow_instance:
        admins = await uow_instance.user_repository.get_admins()
        if not admins:
            default_admin_id = settings.telegram_admin_ids[0]
            user_data = User(
                telegram_id=default_admin_id,
                telegram_username='admin',
                name='Admin',
                registered_datetime_utc=datetime.now(settings.tzinfo),
                last_activity_datetime_utc=datetime.now(settings.tzinfo),
                isbanned=False,
                status=UserStatus.ADMIN
            )
            success = await uow_instance.user_repository.add_user(user_data)
            if success:
                bot_requests_logger.info(f'Default admin added: {default_admin_id}')
            else:
                exception_logger.error(f'Failed to add default admin: {default_admin_id}')
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
    await set_initial_admin()
    try:
        groups = await imager_service.list_groups()
        await uow.image_group_repository.sync_groups(groups.groups)
    except Exception:
        exception_logger.exception('Не удалось обновить список библиотек MinIO при запуске')
    stop_notifications = asyncio.Event()
    notifier = GenerationNotifier(imager_service, bot, uow.generation_history_repository)
    notifier_task = asyncio.create_task(notifier.run(stop_notifications), name='generation-notifier')
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot, close_bot_session=False)
    finally:
        stop_notifications.set()
        try:
            await asyncio.wait_for(asyncio.shield(notifier_task), timeout=config.bot.shutdown_seconds)
        except asyncio.TimeoutError:
            notifier_task.cancel()
            with suppress(asyncio.CancelledError):
                await notifier_task
        finally:
            imager_service.close()
            mongo_client.close()
            await bot.session.close()

if __name__ == '__main__':
    asyncio.run(main())
