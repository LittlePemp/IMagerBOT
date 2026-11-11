import os
import math
from typing import List, Tuple

from pydantic import ConfigDict, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    mongodb_uri: str
    database_name: str
    temporal_address: str = 'temporal:7233'
    temporal_namespace: str = 'imager'
    temporal_task_queue: str = 'imager-render'
    temporal_dispatch_poll_seconds: float = Field(default=1, gt=0, le=60)
    temporal_worker_identity: str = ''
    job_execution_lease_seconds: int = Field(default=60, ge=30, le=600)
    minio_endpoint: str = 'minio:9000'
    minio_access_key: SecretStr = SecretStr('')
    minio_secret_key: SecretStr = SecretStr('')
    minio_bucket: str = 'imager'
    minio_secure: bool = False
    rabbitmq_url: str = 'amqp://guest:guest@127.0.0.1:5672/'
    rabbitmq_requests_queue: str = 'imager.requests'
    file_path_prefix: str = ''
    image_groups_relative_path: str = 'files/groups'
    uploaded_images_path: str = 'files/uploaded'
    generated_images_path: str = 'files/generated'
    generated_image_extension: str = 'jpg'
    generated_image_jpeg_quality: int = Field(default=90, ge=1, le=100)
    telegram_max_document_size_bytes: int = Field(default=47185920, gt=0)
    generation_estimated_jpeg_bytes_per_pixel: float = Field(default=1.0, gt=0, allow_inf_nan=False)
    rabbitmq_prefetch_count: int = Field(default=1, ge=1, le=8)
    api_queue_maxsize: int = Field(default=8, ge=1, le=100)
    api_request_timeout_seconds: float = Field(default=600, gt=0, le=3600)
    api_shutdown_timeout_seconds: float = Field(default=30, gt=0, le=300)
    rabbitmq_max_retries: int = Field(default=2, ge=0, le=5)
    rpc_result_ttl_seconds: int = Field(default=86400, ge=60, le=604800)
    rpc_result_max_entries: int = Field(default=1000, ge=1, le=10000)

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

    @model_validator(mode='after')
    def validate_image_policy(self):
        if any(value <= 0 for value in (*self.min_size, *self.max_size)):
            raise ValueError('Image dimensions must be positive')
        if any(low > high for low, high in zip(self.min_size, self.max_size)):
            raise ValueError('Minimum image dimensions must not exceed maximum dimensions')
        if any(value > 4000 for value in self.max_size):
            raise ValueError('Input image dimensions must fit the 4000 pixel decoder safety ceiling')
        low, high = self.aspect_ratio_limits
        if not (math.isfinite(low) and math.isfinite(high) and 0 < low <= high):
            raise ValueError('Aspect ratio limits must be finite, positive and ordered')
        if self.generated_image_extension.lower().lstrip('.') not in ('jpg', 'jpeg', 'png'):
            raise ValueError('Output format must be jpg, jpeg or png')
        return self

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
