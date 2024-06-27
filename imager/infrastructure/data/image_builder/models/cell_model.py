from assimilator.mongo.database.models import MongoModel
from pydantic import Field
from bson import ObjectId


class CellModel(MongoModel):
    id: ObjectId = Field(default_factory=ObjectId, alias='_id')
    r: int
    g: int
    b: int
    group: str
    relative_file_path: str

    class AssimilatorConfig:
        collection = 'cell_configurations'
