from aiogram import Dispatcher
from aiogram.filters import Command
from aiogram.types import Message
from src.infrastructure.data.repositories.user_repository import UserRepository

async def make_admin(message: Message, user_repository: UserRepository, user):
    if not user.is_admin:
        await message.answer('У вас нет доступа к этой команде.')
        return

    try:
        user_id = int(message.get_args())
        target_user = await user_repository.get_user_by_id(user_id)
        if target_user:
            await user_repository.update_user(user_id, {"status": "admin"})
            await message.answer(f'Пользователь с ID {user_id} теперь администратор.')
        else:
            await message.answer(f'Пользователь с ID {user_id} не найден.')
    except ValueError:
        await message.answer('Пожалуйста, предоставьте действительный Telegram ID.')

def register_handlers_user_management(dp: Dispatcher, user_repository: UserRepository):
    dp.message.register(lambda message, data: make_admin(message, user_repository, data['user']), Command('make_admin'))
