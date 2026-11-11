from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class GenerateImageRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')

    image_path: str = Field(min_length=1, max_length=4096)
    group_name: str = Field(min_length=1, max_length=128)
    insertion_format: Literal['crop', 'scale'] = 'crop'
    alpha_channel: int = Field(default=30, strict=True, ge=0, le=100)
    noise_level: int = Field(default=15, strict=True, ge=0, le=100)
    cell_size: int = Field(default=40, strict=True, gt=0)
    result_size: int = Field(default=120, strict=True, gt=0)
    output_name: str | None = Field(default=None, min_length=1, max_length=200)


class GenerateImageResponse(BaseModel):
    path: str


class GroupInfo(BaseModel):
    group: str
    count: int

class ListGroupsResponse(BaseModel):
    groups: list[GroupInfo]


class SyncGroupsResponse(BaseModel):
    message: str
