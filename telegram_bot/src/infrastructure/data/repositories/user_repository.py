from typing import Optional

from src.infrastructure.data.models.user import User


class UserRepository:
    def __init__(self, db):
        self.collection = db['users']

    async def add_user(self, user: User):
        await self.collection.insert_one(user.model_dump())

    async def get_user_by_id(self, telegram_id: int) -> Optional[User]:
        user_data = await self.collection.find_one({'telegram_id': telegram_id})
        if user_data:
            return User(**user_data)
        return None

    async def update_user(self, telegram_id: int, update_data: dict):
        await self.collection.update_one({'telegram_id': telegram_id}, {'$set': update_data})
