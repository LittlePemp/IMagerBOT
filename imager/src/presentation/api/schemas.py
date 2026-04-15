from pydantic import BaseModel


class GenerateImageRequest(BaseModel):
    image_path: str
    group_name: str
    insertion_format: str = 'crop'
    alpha_channel: int = 30
    noise_level: int = 15
    cell_size: int = 40
    result_size: int = 120
    output_name: str | None = None


class GenerateImageResponse(BaseModel):
    path: str


class GroupInfo(BaseModel):
    group: str
    count: int

class ListGroupsResponse(BaseModel):
    groups: list[GroupInfo]


class SyncGroupsResponse(BaseModel):
    message: str
