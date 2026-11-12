from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field, ValidationError, field_validator

from imager_contracts.protocol import GROUP_NAME_MAX_LENGTH
from settings import settings
from src.models.image_build_params import validate_display_name
from src.utils.building_blocks.result import Result


class GroupStatus(str, Enum):
    ACTIVE = 'active'
    INACTIVE = 'inactive'


class ImageGroup(BaseModel):
    imager_name: str = Field(min_length=1, max_length=GROUP_NAME_MAX_LENGTH)
    display_name: str
    count: int = Field(ge=0)
    status: GroupStatus = GroupStatus.ACTIVE
    available_in_core: bool = True
    revision: int = Field(default=0, ge=0)
    last_synced: datetime

    _validate_name = field_validator('display_name', mode='before')(validate_display_name)

    @classmethod
    def create(cls, imager_name: str, count: int) -> Result:
        try:
            return Result.Success(cls(
                imager_name=imager_name, display_name=imager_name, count=count,
                last_synced=datetime.now(settings.tzinfo),
            ))
        except ValidationError as error:
            return Result.Error(str(error))

    @property
    def active(self):
        return self.status == GroupStatus.ACTIVE and self.available_in_core and self.count > 0
