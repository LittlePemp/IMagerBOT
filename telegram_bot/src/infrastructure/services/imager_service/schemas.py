from pydantic import BaseModel, ConfigDict, Field


class GenerateImageRequest(BaseModel):
    """Values resolved from the user's current selection, never local paths."""
    model_config = ConfigDict(extra='forbid')

    source: dict
    group_name: str = Field(min_length=1, max_length=128)
    insertion_format: str = 'crop'
    alpha_channel: int = Field(default=30, strict=True, ge=0, le=100)
    noise_level: int = Field(default=15, strict=True, ge=0, le=100)
    cell_size: int = Field(default=40, strict=True, gt=0)
    result_size: int = Field(default=120, strict=True, gt=0)


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
