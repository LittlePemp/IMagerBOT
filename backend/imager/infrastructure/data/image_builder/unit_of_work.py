from assimilator.mongo.database import MongoUnitOfWork, MongoRepository
from pymongo import MongoClient
from imager.infrastructure.data.image_builder.models.cell_model import CellModel  # noqa
from imager.infrastructure.data.image_builder.models.cell_object_model import CellObjectModel  # noqa
from imager.infrastructure.data.image_builder.repositories.cell_repository import CellRepository  # noqa
from imager.infrastructure.data.image_builder.repositories.cell_object_repository import CellObjectRepository  # noqa


class MongoUnitOfWork(MongoUnitOfWork):
    def __init__(self, settings):
        client = MongoClient(settings.mongodb_uri)
        cell_repository = MongoRepository(
            session=client,
            model=CellModel,
            database='assimilator_database',
        )
        cell_object_repository = MongoRepository(
            session=client,
            model=CellObjectModel,
            database='assimilator_database',
        )
        super().__init__([cell_repository, cell_object_repository])
        self.cell_repository = CellRepository(self)
        self.cell_object_repository = CellObjectRepository(self)
        self.settings = settings


def get_uow(settings):
    return MongoUnitOfWork(settings)
