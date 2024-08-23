from bson import ObjectId
from pydantic import BaseModel, ConfigDict, Field, field_validator


class CellModel(BaseModel):
    id: ObjectId = Field(default_factory=ObjectId, alias='_id')
    r: int
    g: int
    b: int
    group: str
    relative_file_path: str

    model_config = ConfigDict(
        populate_by_name=True,
        arbitrary_types_allowed=True,
        json_encoders={
            ObjectId: str
        }
    )

    @field_validator('r', 'g', 'b')
    def validate_rgb(cls, v):
        if not (0 <= v <= 255):
            raise ValueError('RGB values must be between 0 and 255')
        return v
