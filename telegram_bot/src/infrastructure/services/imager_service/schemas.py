from pydantic import BaseModel, ConfigDict, Field

from imager_config.loader import config
from imager_contracts.protocol import GROUP_NAME_MAX_LENGTH, PERCENT_MAX


class GenerateImageRequest(BaseModel):
    """Values resolved from the user's current selection, never local paths."""
    model_config = ConfigDict(extra='forbid')

    source: dict
    group_name: str = Field(min_length=1, max_length=GROUP_NAME_MAX_LENGTH)
    insertion_format: str = config.generation.insertion_format
    alpha_channel: int = Field(default=config.generation.alpha_channel, strict=True, ge=0, le=PERCENT_MAX)
    noise_level: int = Field(default=config.generation.noise_level, strict=True, ge=0, le=PERCENT_MAX)
    cell_size: int = Field(default=config.generation.cell_size, strict=True, gt=0)
    result_size: int = Field(default=config.generation.result_size, strict=True, gt=0)


class GenerationAccepted(BaseModel):
    job_id: str
    status: str
    workflow_id: str


class GroupInfo(BaseModel):
    group: str
    count: int


class ListGroupsResponse(BaseModel):
    groups: list[GroupInfo]


class SyncGroupsResponse(BaseModel):
    message: str
