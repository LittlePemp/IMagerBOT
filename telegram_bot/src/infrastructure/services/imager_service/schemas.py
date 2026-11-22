from pydantic import BaseModel, ConfigDict, Field, field_validator

from imager_config.loader import config
from imager_contracts import validate_watermark_text
from imager_contracts.protocol import GROUP_NAME_MAX_LENGTH, PERCENT_MAX


class GenerateImageRequest(BaseModel):
    """Values resolved from the user's current selection, never local paths."""
    model_config = ConfigDict(extra='forbid')

    source: dict
    watermark_text: str | None = None
    group_name: str = Field(min_length=1, max_length=GROUP_NAME_MAX_LENGTH)
    insertion_format: str = config.generation.insertion_format
    alpha_channel: int = Field(default=config.generation.alpha_channel, strict=True, ge=0, le=PERCENT_MAX)
    noise_level: int = Field(default=config.generation.noise_level, strict=True, ge=0, le=PERCENT_MAX)
    cell_size: int = Field(default=config.generation.cell_size, strict=True, gt=0)
    result_size: int = Field(default=config.generation.result_size, strict=True, gt=0)

    @field_validator('watermark_text', mode='before')
    @classmethod
    def watermark_is_bounded_text(cls, value):
        return validate_watermark_text(value)


class GenerationAccepted(BaseModel):
    job_id: str
    status: str
    workflow_id: str


class GroupInfo(BaseModel):
    group: str
    count: int
    display_name: str | None = None


class ListGroupsResponse(BaseModel):
    groups: list[GroupInfo]


class SyncGroupsResponse(BaseModel):
    message: str
