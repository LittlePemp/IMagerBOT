from assimilator.mongo.database import MongoRepository
from assimilator.mongo.database import MongoUnitOfWork as BaseMongoUnitOfWork
from imager.infrastructure.data.image_builder.models.cell_model import \
    CellModel
from imager.infrastructure.data.image_builder.models.cell_object_model import \
    CellObjectModel
from imager.infrastructure.data.image_builder.repositories.cell_object_repository import \
    CellObjectRepository
from imager.infrastructure.data.image_builder.repositories.cell_repository import \
    CellRepository
from imager.infrastructure.data.image_builder.repositories.file_repository import \
    FileRepository
from pymongo import MongoClient
from settings import settings


class MongoUnitOfWork(BaseMongoUnitOfWork):
    def __init__(self):
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
        self.cell_repository = CellRepository(cell_repository)
        self.cell_object_repository = CellObjectRepository(cell_object_repository)
        self.file_repository = FileRepository()
        self.settings = settings


def get_uow():
    return MongoUnitOfWork()
