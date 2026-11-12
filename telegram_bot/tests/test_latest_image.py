"""User progress can be found by ownership without exposing internal IDs."""
import copy
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from imager_config.loader import config
from src.infrastructure.data.repositories.admission_repository import AdmissionRepository
from src.infrastructure.services.imager_service.imager_service import ImagerService


class LatestImageRepositoryTests(unittest.IsolatedAsyncioTestCase):
    def repository(self, collection):
        collection.with_options.return_value = collection
        return AdmissionRepository(collection, timezone='Europe/Moscow', daily_limit=5,
                                   lease_seconds=60, retry_seconds=5)

    async def test_latest_skips_empty_days_and_foreign_users(self):
        buckets = [
            {'actor_id': 7, 'date': '2026-11-12', 'admissions': [
                {'job_id': 'older', 'state': 'sent', 'payload_hash': 'h'}]},
            {'actor_id': 8, 'date': '2026-11-14', 'admissions': [
                {'job_id': 'foreign', 'state': 'sent', 'payload_hash': 'h'}]},
            {'actor_id': 7, 'date': '2026-11-14', 'admissions': []},
            {'actor_id': 7, 'date': '2026-11-13', 'admissions': [
                {'job_id': 'first-today', 'state': 'sent', 'payload_hash': 'h1'},
                {'job_id': 'last-today', 'state': 'pending', 'payload_hash': 'h2'}]},
        ]
        collection = Mock()

        async def find_one(query, projection, *, sort):
            self.assertEqual(sort, [('date', -1)])
            self.assertEqual(query.get('admissions.0'), {'$exists': True})
            self.assertEqual(projection.get('admissions'), {'$slice': -1})
            matching = [bucket for bucket in buckets if bucket['actor_id'] == query['actor_id'] and bucket['admissions']]
            return copy.deepcopy(max(matching, key=lambda bucket: bucket['date'])) if matching else None

        collection.find_one = AsyncMock(side_effect=find_one)
        repository = self.repository(collection)
        receipt = await repository.latest(7)
        self.assertEqual(receipt.job_id, 'last-today')
        self.assertEqual(receipt.actor_id, 7)
        self.assertEqual(receipt.state, 'pending')
        self.assertIsNone(await repository.latest(9))

    async def test_unexpected_owner_is_not_returned(self):
        collection = Mock()
        collection.find_one = AsyncMock(return_value={
            'actor_id': 8, 'date': '2026-11-13',
            'admissions': [{'job_id': 'foreign', 'state': 'sent', 'payload_hash': 'h'}],
        })
        self.assertIsNone(await self.repository(collection).latest(7))

    async def test_legacy_lookup_is_scoped_to_the_telegram_owner(self):
        collection = SimpleNamespace(find_one=Mock(return_value=None))
        gateway = ImagerService(SimpleNamespace(), database={config.mongo.jobs_collection: collection})
        self.assertIsNone(await gateway.latest_user_job(7))
        collection.find_one.assert_called_once_with(
            {'request.client_id': 'telegram', 'request.client_context.user_id': 7},
            sort=[('created_at', -1)],
        )
