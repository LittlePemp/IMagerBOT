from assimilator.mongo.database import MongoRepository
from assimilator.mongo.database import MongoUnitOfWork as BaseMongoUnitOfWork
from imager.infrastructure.data.image_builder.models.cell_model import CellModel
from imager.infrastructure.data.image_builder.repositories.cell_repository import CellRepository
from imager.infrastructure.data.image_builder.repositories.file_repository import FileRepository
from pymongo import MongoClient
from settings import settings


class MongoUnitOfWork(BaseMongoUnitOfWork):
    def __init__(self):
        client = MongoClient(settings.mongodb_uri)
        cell_repository = CellRepository(
            MongoRepository(
                session=client,
                model=CellModel,
                database=settings.database_name,
            ),
            FileRepository()
        )
        super().__init__([cell_repository])
        self.file_repository = FileRepository()
        self.cell_repository = cell_repository

    def initialize_repositories(self):
        self.cell_repository.initialize()


def get_uow():
    uow = MongoUnitOfWork()
    uow.initialize_repositories()
    return uow
