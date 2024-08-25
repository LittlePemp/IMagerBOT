from datetime import datetime

from settings import settings
from src.models.user import User, UserStatus
from src.utils.loggers import bot_requests_logger, exception_logger


async def set_initial_admin(uow):
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
