import unittest
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Lock
from unittest.mock import MagicMock, patch

from src.infrastructure.data.image_builder.unit_of_work import MongoUnitOfWork, get_uow


class TestMongoUnitOfWork(unittest.TestCase):
    def setUp(self):
        MongoUnitOfWork._instance = None
        self.addCleanup(setattr, MongoUnitOfWork, '_instance', None)
        self.repository_patch = patch('src.infrastructure.data.image_builder.unit_of_work.CellRepository')
        self.repository_patch.start()
        self.addCleanup(self.repository_patch.stop)
        self.client = MagicMock()

    def test_singleton_shares_client_and_initializes_once(self):
        first = MongoUnitOfWork(client=self.client)
        second = MongoUnitOfWork(client=MagicMock())
        self.assertIs(first, second)
        self.assertIs(first.client, self.client)
        first.cell_repository.initialize.assert_called_once()

    def test_context_does_not_claim_unused_mongo_transaction(self):
        with MongoUnitOfWork(client=self.client):
            pass
        self.client.start_session.assert_not_called()

    def test_context_releases_lock_after_error_and_allows_nested_use(self):
        instance = MongoUnitOfWork(client=self.client)
        with self.assertRaisesRegex(RuntimeError, 'failure'):
            with instance:
                raise RuntimeError('failure')
        with instance:
            with instance:
                self.assertIs(get_uow(), instance)

    def test_concurrent_contexts_are_serialized(self):
        instance = MongoUnitOfWork(client=self.client)
        barrier, lock = Barrier(2), Lock()
        active = peak = 0

        def operation():
            nonlocal active, peak
            barrier.wait(timeout=2)
            with instance:
                with lock:
                    active += 1
                    peak = max(peak, active)
                time.sleep(0.01)
                with lock:
                    active -= 1

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(operation) for _ in range(2)]
            for future in futures:
                future.result(timeout=3)
        self.assertEqual(peak, 1)
