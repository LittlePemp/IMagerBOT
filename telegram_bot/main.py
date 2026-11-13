"""Composition root. Importing this module does not create clients or start polling."""
import asyncio
import logging
import sys
from datetime import datetime
from pathlib import Path

_REPOSITORY_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPOSITORY_ROOT not in sys.path:
    sys.path.insert(1, _REPOSITORY_ROOT)

from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.fsm.storage.memory import SimpleEventIsolation
from aiogram.fsm.storage.pymongo import PyMongoStorage
from pymongo import AsyncMongoClient
from loguru import logger

from imager_config.loader import config
from settings import settings
from src.application.admission import AdmissionDispatcher
from src.application.generation import SubmitGeneration
from src.application.generation_progress import GenerationProgress
from src.application.generation_result_menu import GenerationResultMenu
from src.application.catalog_projection import CatalogImportProjection
from src.commands.about.about_handler import register_handlers_about
from src.commands.admin_panel.admin_panel_handler import register_handlers_admin_panel
from src.commands.admin_panel.user_management import register_handlers_user_management_total
from src.commands.admin_panel.image_build_params.image_build_params_handler import register_handlers_image_build_params
from src.commands.image_generation.image_generation_handler import register_handlers_image_generation
from src.commands.main_menu import register_handlers_main_menu, register_fallback
from src.commands.admin_panel.pack_import import register_handlers_pack_import, register_pack_import_command
from src.commands.support.support_handler import register_handlers_support
from src.infrastructure.data.unit_of_work import get_uow
from src.infrastructure.services.imager_service import imager_service
from src.infrastructure.services.imager_service.notifier import GenerationNotifier
from src.infrastructure.services.catalog_import_service import CatalogImportService
from src.models.user import User, UserStatus
from src.utils.loggers import exception_logger
from src.utils.middlewares import logging_middleware
from src.utils.middlewares.abuse_control import AbuseControlMiddleware, UploadGate
from src.utils.middlewares.authentication_middleware import AuthenticationMiddleware
from src.utils.middlewares.errors import ErrorBoundaryMiddleware
from src.utils.navigation import ConversationExpiryMiddleware, NavigationMiddleware


class SharedClientStorage(PyMongoStorage):
    async def close(self):
        # main owns this client and closes it after background consumers stop.
        pass


def create_dispatcher(repositories, gateway, *, storage=None, upload_gate=None, progress=None):
    gate = upload_gate or UploadGate()
    # Register FSM after the cheap abuse gate, before database-backed actor/UI.
    dp = Dispatcher(storage=storage, events_isolation=SimpleEventIsolation(), disable_fsm=True)
    dp['uow'] = repositories
    dp['user_repository'] = repositories.user_repository
    dp['catalog_import_service'] = CatalogImportService(gateway, upload_gate=gate)
    dp['generation_service'] = SubmitGeneration(
        repositories, gateway, gate, fixed_admin_ids=settings.telegram_admin_ids,
    )
    dp['generation_progress'] = progress
    dp.update.outer_middleware(ErrorBoundaryMiddleware())
    dp.update.outer_middleware(AbuseControlMiddleware())
    dp.update.outer_middleware(dp.fsm)
    dp.update.outer_middleware(logging_middleware)
    for observer in (dp.message, dp.callback_query):
        observer.outer_middleware(AuthenticationMiddleware(repositories.user_repository))
    dp.callback_query.outer_middleware(NavigationMiddleware())
    dp.message.outer_middleware(ConversationExpiryMiddleware())
    register_handlers_main_menu(dp)
    register_pack_import_command(dp)
    register_handlers_about(dp)
    register_handlers_support(dp)
    register_handlers_admin_panel(dp)
    register_handlers_user_management_total(dp)
    register_handlers_image_build_params(dp, repositories)
    register_handlers_pack_import(dp, with_command=False)
    register_handlers_image_generation(dp, repositories)
    register_fallback(dp)
    return dp


async def set_initial_admin(repositories):
    for admin_id in settings.telegram_admin_ids:
        user = await repositories.user_repository.get_user_by_id(admin_id)
        if user is None:
            await repositories.user_repository.add_user(User(
                telegram_id=admin_id, telegram_username=None, name=None,
                registered_datetime_utc=datetime.now(settings.tzinfo),
                last_activity_datetime_utc=datetime.now(settings.tzinfo),
                isbanned=False, status=UserStatus.ADMIN,
            ))
        elif not user.is_admin or user.isbanned:
            await repositories.user_repository.update_user(admin_id, {'status': UserStatus.ADMIN, 'isbanned': False})


async def main():
    logging.basicConfig(level=logging.INFO)
    bot = Bot(token=settings.api_token.get_secret_value(), session=AiohttpSession(
        timeout=config.bot.api_timeout_seconds,
        api=TelegramAPIServer.from_base(settings.telegram_api_base_url, is_local=settings.telegram_api_local),
    ))
    client = AsyncMongoClient(
        settings.mongodb_uri, tz_aware=True,
        serverSelectionTimeoutMS=config.mongo.server_selection_timeout_ms,
        connectTimeoutMS=config.mongo.connect_timeout_ms,
        socketTimeoutMS=config.mongo.socket_timeout_ms,
        waitQueueTimeoutMS=config.mongo.wait_queue_timeout_ms,
    )
    stop, tasks = asyncio.Event(), []
    try:
        repositories = get_uow(client[settings.database_name])
        await repositories.initialize_indexes()
        await client[settings.database_name][config.mongo.bot_fsm_collection].create_index(
            'data._current_view.expires_on', expireAfterSeconds=0,
        )
        await set_initial_admin(repositories)
        try:
            await imager_service.ensure_indexes()
            await repositories.image_group_repository.sync_groups((await imager_service.list_groups()).groups)
        except Exception:
            exception_logger.exception('Core catalog unavailable during bot startup')
        gate = UploadGate()
        progress = GenerationProgress(
            repositories.generation_progress_repository, repositories.admission_repository,
            imager_service, bot,
        )
        dispatcher = create_dispatcher(repositories, imager_service, upload_gate=gate, storage=SharedClientStorage(
            client, db_name=settings.database_name, collection_name=config.mongo.bot_fsm_collection,
        ), progress=progress)
        imports = dispatcher['catalog_import_service']
        await imports.ensure_indexes()
        result_menu = GenerationResultMenu(
            dispatcher, repositories.user_repository, bot,
            fixed_admin_ids=settings.telegram_admin_ids,
        )
        notifier = GenerationNotifier(
            imager_service, bot, repositories.generation_history_repository,
            progress=progress, on_result_delivered=result_menu,
        )
        admission = AdmissionDispatcher(repositories.admission_repository, imager_service, publication_lock=gate.admission_lock)
        projection = CatalogImportProjection(imports, repositories.image_group_repository)
        tasks = [asyncio.create_task(notifier.run(stop)), asyncio.create_task(admission.run(stop)),
                 asyncio.create_task(projection.run(stop)), asyncio.create_task(progress.run(stop))]
        await bot.delete_webhook(drop_pending_updates=False)
        await dispatcher.start_polling(
            bot, close_bot_session=False, allowed_updates=['message', 'callback_query'],
            tasks_concurrency_limit=config.bot.max_concurrent_updates,
        )
    finally:
        stop.set()
        try:
            if tasks:
                try:
                    await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), config.bot.shutdown_seconds)
                except asyncio.TimeoutError:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            try:
                imager_service.close()
            finally:
                try:
                    await client.close()
                finally:
                    await bot.session.close()
                    await logger.complete()


if __name__ == '__main__':
    asyncio.run(main())
