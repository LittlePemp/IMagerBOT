"""Resolve a fresh principal for both messages and callbacks."""
from aiogram import BaseMiddleware
from settings import settings
from src.models.user import User, UserStatus


class AuthenticationMiddleware(BaseMiddleware):
    def __init__(self, user_repository):
        self.user_repository = user_repository

    async def __call__(self, handler, event, data):
        if event.from_user is None:
            return
        profile = event.from_user
        user = await self.user_repository.get_user_by_id(profile.id)
        fixed = profile.id in settings.telegram_admin_ids
        if user is None:
            created = User.create(
                telegram_id=profile.id, telegram_username=profile.username,
                name=profile.full_name, isbanned=False,
                status=UserStatus.ADMIN if fixed else UserStatus.USER,
            )
            if not created.is_success:
                raise ValueError('Invalid Telegram profile')
            await self.user_repository.add_user(created.value)
            user = await self.user_repository.get_user_by_id(profile.id)
            if user is None:
                raise RuntimeError('Cannot confirm user registration')
        if fixed:
            user = user.model_copy(update={'status': UserStatus.ADMIN, 'isbanned': False})
        if user.isbanned:
            await event.answer('Вы заблокированы и не можете использовать этого бота.')
            return
        await self.user_repository.update_user_info(profile.id, profile.username, profile.full_name)
        data['user'] = user
        data['user_repository'] = self.user_repository
        return await handler(event, data)
