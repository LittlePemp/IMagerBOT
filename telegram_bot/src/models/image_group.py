from datetime import datetime
from pydantic import BaseModel, ValidationError
from enum import Enum

from src.utils.building_blocks.result import Result
from settings import settings

class GroupStatus(str, Enum):
    ACTIVE = 'active'
    INACTIVE = 'inactive'

class ImageGroup(BaseModel):
    imager_name: str
    display_name: str
    count: int
    status: GroupStatus = GroupStatus.ACTIVE
    last_synced: datetime

    @classmethod
    def create(cls, imager_name: str, count: int) -> Result:
        try:
            group = cls(
                imager_name=imager_name,
                display_name=imager_name,  # default value
                count=count,
                last_synced=datetime.now(settings.tzinfo),
            )
            return Result.Success(group)
        except ValidationError as e:
            return Result.Error(f'Validation error: {e}')

    class Config:
        arbitrary_types_allowed = True
