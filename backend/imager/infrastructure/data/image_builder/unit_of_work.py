from assimilator.mongo.database import MongoRepository, MongoUnitOfWork
from pymongo import MongoClient

from .models.cell_model import CellModel
from .models.cell_object_model import CellObjectModel
from .repositories.cell_object_repository import CellObjectRepository
from .repositories.cell_repository import CellRepository


class MyMongoUnitOfWork(MongoUnitOfWork):
    def __init__(self, settings, image_service):
        client = MongoClient(settings.mongodb_uri)
        cell_repository = MongoRepository(
            session=client,
            model=CellModel,
            database=settings.database_name,
        )
        cell_object_repository = MongoRepository(
            session=client,
            model=CellObjectModel,
            database=settings.database_name,
        )
        super().__init__([cell_repository, cell_object_repository])
        self.cell_repository = CellRepository(cell_repository, image_service)
        self.cell_object_repository = CellObjectRepository(cell_object_repository)
        self.settings = settings


def get_uow(settings, image_service):
    return MyMongoUnitOfWork(settings, image_service)
