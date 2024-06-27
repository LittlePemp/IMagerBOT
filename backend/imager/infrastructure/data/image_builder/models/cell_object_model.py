from assimilator.mongo.database.models import MongoModel
from pydantic import Field
from bson import ObjectId


class CellObjectModel(MongoModel):
    id: ObjectId = Field(default_factory=ObjectId, alias='_id')
    cell_id: ObjectId
    image_data: bytes

    class AssimilatorConfig:
        collection = 'cell_objects'
