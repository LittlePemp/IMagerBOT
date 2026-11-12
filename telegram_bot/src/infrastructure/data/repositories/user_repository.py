"""User identity and access persisted with atomic updates, without a shared session."""
import re
from datetime import datetime
from typing import Optional

from pymongo.asynchronous.collection import AsyncCollection
from pymongo.errors import DuplicateKeyError

from settings import settings
from src.infrastructure.data.errors import (
    AccessDenied, ProtectedAdministrator, UserConflict, UserNotFound,
)
from src.models.user import User, UserStatus
from src.utils.loggers import service_logger
from src.utils.loggers import db_logger


class UserRepository:
    def __init__(self, collection: AsyncCollection):
        self.collection = collection

    async def initialize_indexes(self):
        try:
            await self.collection.create_index('telegram_id', unique=True)
            await self.collection.create_index('username_normalized')
        except DuplicateKeyError as error:
            raise RuntimeError(
                'Duplicate Telegram identities exist; resolve them before starting the bot.'
            ) from error

    async def add_user(self, user: User) -> bool:
        document = user.model_dump()
        document['username_normalized'] = self._username(user.telegram_username)
        try:
            await self.collection.update_one(
                {'telegram_id': user.telegram_id}, {'$setOnInsert': document}, upsert=True,
            )
        except DuplicateKeyError:
            # Another first update already created the same identity.
            if await self.collection.find_one({'telegram_id': user.telegram_id}) is None:
                raise
        return True

    async def get_user_by_id(self, telegram_id: int) -> Optional[User]:
        document = await self.collection.find_one({'telegram_id': telegram_id})
        return User(**document) if document else None

    @staticmethod
    def _username(username):
        return username.strip().lstrip('@').casefold() if username else None

    async def get_user_by_username(self, username: str) -> Optional[User]:
        normalized = self._username(username)
        if not normalized:
            return None
        document = await self.collection.find_one({'$or': [
            {'username_normalized': normalized},
            {'telegram_username': {'$regex': f'^{re.escape(normalized)}$', '$options': 'i'}},
        ]})
        return User(**document) if document else None

    @staticmethod
    def _revision_filter(revision):
        return ({'$or': [{'revision': 0}, {'revision': {'$exists': False}}]}
                if revision == 0 else {'revision': revision})

    async def update_user(self, telegram_id: int, update_data: dict, *, expected_revision=None) -> bool:
        if 'telegram_id' in update_data:
            raise ValueError('Telegram identity cannot be changed')
        fields = dict(update_data)
        if 'telegram_username' in fields:
            fields['username_normalized'] = self._username(fields['telegram_username'])
        if 'status' in fields:
            fields['status'] = UserStatus(fields['status']).value
        query = {'telegram_id': telegram_id}
        if expected_revision is not None:
            query.update(self._revision_filter(expected_revision))
        update = {'$set': fields}
        if {'status', 'isbanned'} & fields.keys():
            update['$inc'] = {'revision': 1}
        result = await self.collection.update_one(query, update)
        return bool(result.matched_count)

    async def change_access(
        self, actor_id, target_id, *, status=None, isbanned=None,
        expected_revision, fixed_admin_ids,
    ) -> User:
        fixed = frozenset(fixed_admin_ids)
        actor = await self.get_user_by_id(actor_id)
        if actor_id not in fixed and (actor is None or actor.isbanned or not actor.is_admin):
            raise AccessDenied('Недостаточно прав для изменения пользователя.')
        target = await self.get_user_by_id(target_id)
        if target is None:
            raise UserNotFound('Пользователь не найден.')
        if target_id in fixed and (
            (status is not None and UserStatus(status) != UserStatus.ADMIN) or isbanned is True
        ):
            raise ProtectedAdministrator('Нельзя снять роль или заблокировать администратора из настроек.')
        fields = {}
        if status is not None:
            fields['status'] = UserStatus(status).value
        if isbanned is not None:
            if not isinstance(isbanned, bool):
                raise ValueError('Blocked status must be boolean')
            fields['isbanned'] = isbanned
        if not fields:
            raise ValueError('Не задано действие над пользователем.')
        if not await self.update_user(target_id, fields, expected_revision=expected_revision):
            raise UserConflict('Карточка пользователя устарела. Откройте её заново.')
        service_logger.info(
            f'Access changed: actor_id={actor_id}, target_id={target_id}, '
            f'revision={expected_revision}, fields={fields}'
        )
        db_logger.info(
            f'Access changed: actor_id={actor_id}, target_id={target_id}, '
            f'previous_status={target.status.value}, previous_banned={target.isbanned}, '
            f'new_values={fields}, expected_revision={expected_revision}'
        )
        updated = await self.get_user_by_id(target_id)
        if updated is None:
            raise UserNotFound('Пользователь больше не существует.')
        return updated

    async def update_last_activity(self, telegram_id: int) -> bool:
        return await self.update_user(
            telegram_id, {'last_activity_datetime_utc': datetime.now(settings.tzinfo)},
        )

    async def update_user_info(self, user_id: int, username: Optional[str], full_name: str) -> bool:
        return await self.update_user(user_id, {
            'telegram_username': username, 'name': full_name,
            'last_activity_datetime_utc': datetime.now(settings.tzinfo),
        })

    async def get_user_count(self) -> int:
        return await self.collection.count_documents({})

    async def get_admins(self) -> list[User]:
        documents = await self.collection.find({'status': UserStatus.ADMIN.value}).to_list(length=None)
        return [User(**document) for document in documents]

    async def get_generation_preferences(self, telegram_id: int) -> Optional[dict]:
        document = await self.collection.find_one(
            {'telegram_id': telegram_id}, {'last_manual_generation': 1, '_id': 0},
        )
        return document.get('last_manual_generation') if document else None

    async def update_generation_preferences(self, telegram_id: int, preferences: dict) -> bool:
        result = await self.collection.update_one(
            {'telegram_id': telegram_id}, {'$set': {'last_manual_generation': preferences}},
        )
        return bool(result.matched_count)
