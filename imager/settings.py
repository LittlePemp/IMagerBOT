import os
from typing import List, Tuple

from pydantic import ConfigDict, field_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    mongodb_uri: str
    database_name: str
    file_path_prefix: str = ''
    image_groups_relative_path: str = 'files/groups'
    uploaded_images_path: str = 'files/uploaded'
    generated_images_path: str = 'files/generated'

    # Image validation settings
    allowed_formats: List[str] = ['RGB', 'RGBA']
    min_size: Tuple[int, int] = (20, 20)
    max_size: Tuple[int, int] = (4000, 4000)
    aspect_ratio_limits: Tuple[float, float] = (9 / 16, 16 / 9)

    _directories_created: bool = False

    model_config = ConfigDict(
        env_file='.env',
        env_file_encoding='utf-8'
    )

    @field_validator('mongodb_uri', 'database_name', mode='before')
    def validate_non_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError('This field cannot be empty')
        return v

    @field_validator('allowed_formats', mode='before')
    def validate_allowed_formats(cls, v: List[str]) -> List[str]:
        if not v or len(v) < 1:
            raise ValueError('There must be at least one allowed format')
        return v

    def create_directories(self) -> None:
        if not self._directories_created:
            os.makedirs(self.image_groups_relative_path, exist_ok=True)
            os.makedirs(self.uploaded_images_path, exist_ok=True)
            os.makedirs(self.generated_images_path, exist_ok=True)
            self._directories_created = True

settings = Settings()

def init_app():
    ''' For main.py initialization '''
    settings.create_directories()
