from datetime import datetime
from typing import Optional

from motor.motor_asyncio import AsyncIOMotorCollection
from settings import settings
from src.infrastructure.data.models.user import User
from src.utils.building_blocks.result import Result
from src.utils.loggers import db_logger, exception_logger


class UserRepository:
    _instance = None

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super(UserRepository, cls).__new__(cls)
        return cls._instance

    def __init__(self, collection: AsyncIOMotorCollection):
        if hasattr(self, '_initialized') and self._initialized:
            return
        self.collection = collection
        self._initialized = True

    async def add_user(self, user: User) -> bool:
        try:
            await self.collection.insert_one(user.model_dump())
            db_logger.info(f'User added: {user}')
            return True
        except Exception as e:
            exception_logger.error(f'Failed to add user: {e}')
            return False

    async def get_user_by_id(self, telegram_id: int) -> Optional[User]:
        try:
            user_data = await self.collection.find_one({'telegram_id': telegram_id})
            if user_data:
                user_data.pop('_id', None)
                return User(**user_data)
            return None
        except Exception as e:
            exception_logger.error(f'Failed to get user by id {telegram_id}: {e}')
            return None

    async def get_user_by_username(self, username: str) -> Optional[User]:
        try:
            user_data = await self.collection.find_one({'telegram_username': username})
            if user_data:
                user_data.pop('_id', None)
                return User(**user_data)
            return None
        except Exception as e:
            exception_logger.error(f'Failed to get user by username {username}: {e}')
            return None

    async def update_user(self, telegram_id: int, update_data: dict) -> bool:
        try:
            result = await self.collection.update_one({'telegram_id': telegram_id}, {'$set': update_data})
            if result.matched_count:
                db_logger.info(f'User updated {telegram_id}: {update_data}')
                return True
            return False
        except Exception as e:
            exception_logger.error(f'Failed to update user {telegram_id}: {e}')
            return False

    async def update_last_activity(self, telegram_id: int) -> bool:
        try:
            result = await self.collection.update_one(
                {'telegram_id': telegram_id},
                {'$set': {'last_activity_datetime_utc': datetime.now(settings.tzinfo)}}
            )
            if result.matched_count:
                db_logger.info(f'Last activity updated for user {telegram_id}')
                return True
            return False
        except Exception as e:
            exception_logger.error(f'Failed to update last activity for user {telegram_id}: {e}')
            return False

    async def update_user_info(self, user_id: int, username: str, full_name: str) -> bool:
        try:
            update_data = {
                'telegram_username': username,
                'name': full_name,
                'last_activity_datetime_utc': datetime.now(settings.tzinfo)
            }
            result = await self.collection.update_one(
                {'telegram_id': user_id},
                {'$set': update_data}
            )
            if result.matched_count:
                db_logger.info(f'User info updated for user {user_id}')
                return True
            return False
        except Exception as e:
            exception_logger.error(f'Failed to update user info for user {user_id}: {e}')
            return False

    async def get_user_count(self) -> int:
        try:
            users_cnt = await self.collection.count_documents({})
            return users_cnt
        except Exception as e:
            db_logger.error(f'Failed to get admins: {e}')
            return 0

    async def get_admins(self) -> list[User]:
        try:
            admins_data = await self.collection.find({'status': 'admin'}).to_list(length=None)
            admins = []
            for admin_data in admins_data:
                admin_data.pop('_id', None)
                admins.append(User(**admin_data))
            db_logger.info('Fetched admins')
            return admins
        except Exception as e:
            exception_logger.error(f'Failed to get admins: {e}')
            return []
