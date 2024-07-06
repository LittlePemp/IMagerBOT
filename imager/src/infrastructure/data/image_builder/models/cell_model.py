from assimilator.mongo.database.models import MongoModel
from bson import ObjectId
from pydantic import Field


class CellModel(MongoModel):
    id: ObjectId = Field(default_factory=ObjectId, alias='_id')
    r: int
    g: int
    b: int
    group: str
    relative_file_path: str

    class AssimilatorConfig:
        collection = 'cell_configurations'
