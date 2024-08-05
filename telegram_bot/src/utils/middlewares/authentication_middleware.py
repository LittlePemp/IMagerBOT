from datetime import datetime

from aiogram import BaseMiddleware
from aiogram.types import Message
from src.utils.loggers import exception_logger
from settings import settings
from src.infrastructure.data.models.user import User, UserStatus
from src.infrastructure.data.repositories.user_repository import UserRepository
from src.utils.loggers import bot_requests_logger


class AuthenticationMiddleware(BaseMiddleware):
    def __init__(self, user_repository: UserRepository):
        super().__init__()
        self.user_repository = user_repository

    async def __call__(self, handler, event: Message, data: dict):
        user_id = event.from_user.id
        username = event.from_user.username
        full_name = event.from_user.full_name

        user = await self.user_repository.get_user_by_id(user_id)
        if user is None:
            user_data = {
                'telegram_username': username,
                'telegram_id': user_id,
                'name': full_name,
                'registered_datetime_utc': datetime.now(settings.tzinfo),
                'last_activity_datetime_utc': datetime.now(settings.tzinfo),
                'isbanned': False,
                'status': UserStatus.USER
            }
            user_result = User.create(**user_data)
            if user_result.is_success:
                user = user_result.value
                success = await self.user_repository.add_user(user)
                if success:
                    bot_requests_logger.info(f'New user added: {user.telegram_id}')
                    user = await self.user_repository.get_user_by_id(user_id)
                else:
                    exception_logger.error(f'Failed to add new user: {user.telegram_id}')
                    return
            else:
                exception_logger.error(f'Failed to create user: {user_result.error}')
                return
        else:
            await self.user_repository.update_user_info(user_id, username, full_name)

        if user.isbanned:
            exception_logger.warning(f'Blocked user attempted access: {user.telegram_id}')
            await event.answer('Вы заблокированы и не можете использовать этого бота.')
            return

        bot_requests_logger.info(f'Authenticated user: {user.telegram_id}')
        data['user'] = user
        return await handler(event, data)
