from pymongo import MongoClient
from pymongo.client_session import ClientSession
from settings import settings

from .repositories.cell_repository import CellRepository
from .repositories.file_repository import FileRepository


class MongoUnitOfWork:
    _instance = None

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super(MongoUnitOfWork, cls).__new__(cls)
            cls._instance._initialized = False
            cls._instance._initialize(*args, **kwargs)
        return cls._instance

    def _initialize(self, client: MongoClient = None):
        self.client = client or MongoClient(settings.mongodb_uri)
        self.session: ClientSession = None
        self.file_repository = FileRepository()
        self.cell_repository = CellRepository(
            self.client[settings.database_name].cell_configurations,
            self.file_repository
        )
        self.repositories_to_init = [self.cell_repository]

        if not self._initialized:
            self.initialize_repositories()
            self._initialized = True

    def __enter__(self):
        self.session = self.client.start_session()
        self.session.start_transaction()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is None:
            self.session.commit_transaction()
        else:
            self.session.abort_transaction()
        self.session.end_session()

    def initialize_repositories(self):
        for repo in self.repositories_to_init:
            # if hasattr(repo, 'initialize'):
            repo.initialize()


def get_uow():
    return MongoUnitOfWork()
