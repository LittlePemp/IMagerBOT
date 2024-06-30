from assimilator.mongo.database import MongoRepository
from pymongo import MongoClient
from .models.cell_model import CellModel  # noqa
from .models.cell_object_model import CellObjectModel  # noqa
from .repositories.cell_repository import CellRepository  # noqa
from .repositories.cell_object_repository import CellObjectRepository  # noqa


class DBContext:
    def __init__(self, settings):
        client = MongoClient(settings.mongodb_uri)
        self.cell_repository = CellRepository(
            MongoRepository(
                session=client,
                model=CellModel,
                database='assimilator_database'
            )
        )
        self.cell_object_repository = CellObjectRepository(
            MongoRepository(
                session=client,
                model=CellObjectModel,
                database='assimilator_database'
            )
        )
        self.settings = settings

    def initialize_repositories(self):
        self.cell_repository.initialize()
        self.cell_object_repository.initialize()
