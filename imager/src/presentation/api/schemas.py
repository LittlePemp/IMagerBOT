from pydantic import BaseModel


class GenerateImageRequest(BaseModel):
    image_path: str
    group_name: str
    insertion_format: str = 'crop'
    alpha_channel: int = 30
    noise_level: int = 40
    cell_size: int = 60
    result_size: int = 120


class GenerateImageResponse(BaseModel):
    path: str
