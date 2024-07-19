from aiogram import types
from aiogram.dispatcher.middlewares.base import BaseMiddleware
from src.infrastructure.data.repositories.user_repository import UserRepository

class AdminMiddleware(BaseMiddleware):
    def __init__(self, user_repository: UserRepository):
        super(AdminMiddleware, self).__init__()
        self.user_repository = user_repository

    async def __call__(self, handler, event, data):
        user_id = None
        if isinstance(event, types.Message):
            user_id = event.from_user.id
        elif isinstance(event, types.CallbackQuery):
            user_id = event.from_user.id

        if user_id:
            user = await self.user_repository.get_user_by_id(user_id)
            data['is_admin'] = user and user.status == 'admin'
        else:
            data['is_admin'] = False

        return await handler(event, data)
