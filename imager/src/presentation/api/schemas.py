from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from imager_config.loader import config
from imager_contracts.protocol import (
    GROUP_NAME_MAX_LENGTH, LOCAL_PATH_MAX_LENGTH, OUTPUT_NAME_MAX_LENGTH, PERCENT_MAX,
)


class GenerateImageRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')

    image_path: str = Field(min_length=1, max_length=LOCAL_PATH_MAX_LENGTH)
    group_name: str = Field(min_length=1, max_length=GROUP_NAME_MAX_LENGTH)
    insertion_format: Literal['crop', 'scale'] = config.generation.insertion_format
    alpha_channel: int = Field(default=config.generation.alpha_channel, strict=True, ge=0, le=PERCENT_MAX)
    noise_level: int = Field(default=config.generation.noise_level, strict=True, ge=0, le=PERCENT_MAX)
    cell_size: int = Field(default=config.generation.cell_size, strict=True, gt=0)
    result_size: int = Field(default=config.generation.result_size, strict=True, gt=0)
    output_name: str | None = Field(default=None, min_length=1, max_length=OUTPUT_NAME_MAX_LENGTH)


class GenerateImageResponse(BaseModel):
    path: str


class GroupInfo(BaseModel):
    group: str
    count: int

class ListGroupsResponse(BaseModel):
    groups: list[GroupInfo]


class SyncGroupsResponse(BaseModel):
    message: str
