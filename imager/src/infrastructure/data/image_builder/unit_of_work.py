from threading import RLock, local

from pymongo import MongoClient
from settings import settings
from src.shared_kernel.resource_limits import generation_slot

from .repositories.cell_repository import CellRepository
from .repositories.file_repository import FileRepository


class MongoUnitOfWork:
    """Shared client/catalog with serialized, idempotent maintenance operations.

    Files and cache do not participate in Mongo transactions. Individual upserts
    are atomic; partial synchronization reports failure and refreshes the cache.
    """

    _instance = None
    _instance_lock = RLock()

    def __new__(cls, *args, **kwargs):
        if cls._instance is not None:
            return cls._instance
        with generation_slot(), cls._instance_lock:
            if cls._instance is None:
                instance = super().__new__(cls)
                instance._initialized = False
                instance._initialize(*args, **kwargs)
                cls._instance = instance
        return cls._instance

    def _initialize(self, client: MongoClient = None):
        self.client = client if client is not None else MongoClient(settings.mongodb_uri)
        self._maintenance_lock = RLock()
        self._contexts = local()
        self.file_repository = FileRepository()
        self.cell_repository = CellRepository(
            self.client[settings.database_name].cell_configurations, self.file_repository
        )
        self.repositories_to_init = [self.cell_repository]
        self.initialize_repositories()
        self._initialized = True

    def __enter__(self):
        slot = generation_slot()
        slot.__enter__()
        try:
            self._maintenance_lock.acquire()
            if not hasattr(self._contexts, 'slots'):
                self._contexts.slots = []
            self._contexts.slots.append(slot)
        except BaseException:
            slot.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        try:
            self._maintenance_lock.release()
        finally:
            self._contexts.slots.pop().__exit__(exc_type, exc_val, exc_tb)

    def initialize_repositories(self):
        for repository in self.repositories_to_init:
            repository.initialize()


def get_uow():
    return MongoUnitOfWork()
