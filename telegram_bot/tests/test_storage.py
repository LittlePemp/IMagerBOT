import asyncio
import copy
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

from pymongo.errors import ConnectionFailure, DuplicateKeyError

from src.infrastructure.data.errors import AccessDenied, ProtectedAdministrator, UserConflict
from src.infrastructure.data.repositories.generation_history_repository import GenerationHistoryRepository
from src.infrastructure.data.repositories.user_repository import UserRepository
from src.infrastructure.data.unit_of_work import MongoRepositories
from src.models.user import User, UserStatus


def user(identifier=1, *, username=None, status=UserStatus.USER):
    return User.create(
        telegram_id=identifier, telegram_username=username, name='User',
        status=status, isbanned=False,
    ).value


class Users:
    def __init__(self):
        self.documents = {}
        self.lock = asyncio.Lock()

    async def find_one(self, query, projection=None):
        return copy.deepcopy(self.documents.get(query.get('telegram_id')))

    async def update_one(self, query, update, upsert=False):
        async with self.lock:
            identifier = query['telegram_id']
            current = self.documents.get(identifier)
            if current is None and upsert:
                current = copy.deepcopy(update['$setOnInsert'])
                self.documents[identifier] = current
            if current is None:
                return SimpleNamespace(matched_count=0)
            expected = query.get('revision')
            if '$or' in query:
                expected = 0
            if expected is not None and current.get('revision', 0) != expected:
                return SimpleNamespace(matched_count=0)
            current.update(copy.deepcopy(update.get('$set', {})))
            for key, value in update.get('$inc', {}).items():
                current[key] = current.get(key, 0) + value
            return SimpleNamespace(matched_count=1)


class TestUserStorage(unittest.IsolatedAsyncioTestCase):
    async def test_missing_username_is_valid_and_concurrent_registration_keeps_one_identity(self):
        collection = Users()
        repository = UserRepository(collection)
        await asyncio.gather(repository.add_user(user()), repository.add_user(user()))
        self.assertEqual(len(collection.documents), 1)
        self.assertIsNone((await repository.get_user_by_id(1)).telegram_username)
        # A later registration attempt cannot overwrite existing access settings.
        await repository.update_user(1, {'status': UserStatus.ADMIN})
        await repository.add_user(user())
        self.assertTrue((await repository.get_user_by_id(1)).is_admin)

    async def test_database_outage_is_not_reported_as_missing_user_or_empty_quota(self):
        collection = SimpleNamespace(
            find_one=AsyncMock(side_effect=ConnectionFailure('offline')),
            count_documents=AsyncMock(side_effect=ConnectionFailure('offline')),
        )
        with self.assertRaises(ConnectionFailure):
            await UserRepository(collection).get_user_by_id(1)
        with self.assertRaises(ConnectionFailure):
            await GenerationHistoryRepository(collection).count_user_records(1)

    async def test_fixed_admin_cannot_be_demoted_or_banned(self):
        collection = Users()
        repository = UserRepository(collection)
        await repository.add_user(user(1, status=UserStatus.ADMIN))
        for change in ({'status': UserStatus.USER}, {'isbanned': True}):
            with self.assertRaises(ProtectedAdministrator):
                await repository.change_access(1, 1, expected_revision=0, fixed_admin_ids=(1,), **change)
        self.assertTrue((await repository.get_user_by_id(1)).is_admin)

    async def test_explicit_actions_are_revision_checked_and_revoked_actor_is_rejected(self):
        collection = Users()
        repository = UserRepository(collection)
        await repository.add_user(user(1, status=UserStatus.ADMIN))
        await repository.add_user(user(2))
        updated = await repository.change_access(
            1, 2, isbanned=True, expected_revision=0, fixed_admin_ids=(1,),
        )
        self.assertTrue(updated.isbanned)
        self.assertEqual(updated.revision, 1)
        with self.assertRaises(UserConflict):
            await repository.change_access(1, 2, isbanned=False, expected_revision=0, fixed_admin_ids=(1,))
        await repository.update_user(1, {'status': UserStatus.USER})
        with self.assertRaises(AccessDenied):
            await repository.change_access(1, 2, isbanned=False, expected_revision=1, fixed_admin_ids=())

    async def test_legacy_user_without_revision_supports_first_cas_and_profile_does_not_invalidate_card(self):
        collection = Users()
        repository = UserRepository(collection)
        await repository.add_user(user(1))
        collection.documents[1].pop('revision')
        self.assertTrue(await repository.update_user(1, {'isbanned': True}, expected_revision=0))
        await repository.update_user_info(1, None, 'New name')
        self.assertEqual((await repository.get_user_by_id(1)).revision, 1)

    async def test_duplicate_id_index_fails_startup_without_deleting_records(self):
        collection = SimpleNamespace(create_index=AsyncMock(side_effect=DuplicateKeyError('duplicate')))
        with self.assertRaisesRegex(RuntimeError, 'Duplicate Telegram identities'):
            await UserRepository(collection).initialize_indexes()

    async def test_compatibility_context_has_no_shared_session_or_transaction_side_effects(self):
        bundle = object.__new__(MongoRepositories)
        first, second = await asyncio.gather(bundle.__aenter__(), bundle.__aenter__())
        self.assertIs(first, bundle)
        self.assertIs(second, bundle)
        self.assertFalse(hasattr(bundle, 'session'))
        self.assertFalse(await bundle.__aexit__(None, None, None))
        self.assertFalse(await bundle.__aexit__(RuntimeError, RuntimeError(), None))
