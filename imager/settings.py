import os
import math
from typing import List, Tuple

from pydantic import ConfigDict, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings
from imager_config.loader import config
from imager_contracts.protocol import OUTPUT_EXTENSIONS, PERCENT_MAX


class Settings(BaseSettings):
    mongodb_uri: str
    database_name: str = config.mongo.core_database
    temporal_address: str = config.temporal.address
    temporal_namespace: str = config.temporal.namespace
    temporal_task_queue: str = config.temporal.task_queue
    temporal_dispatch_poll_seconds: float = Field(default=config.temporal.dispatcher.poll_seconds, gt=0, le=60)
    temporal_worker_identity: str = ''
    job_execution_lease_seconds: int = Field(default=config.jobs.execution_lease_seconds, ge=30, le=600)
    minio_endpoint: str = config.storage.endpoint
    minio_access_key: SecretStr = SecretStr('')
    minio_secret_key: SecretStr = SecretStr('')
    minio_bucket: str = config.storage.bucket
    minio_secure: bool = config.storage.secure
    rabbitmq_url: str = config.legacy.rabbitmq_url
    rabbitmq_requests_queue: str = config.legacy.rabbitmq_requests_queue
    file_path_prefix: str = ''
    image_groups_relative_path: str = config.paths.image_groups
    uploaded_images_path: str = config.paths.uploads
    generated_images_path: str = config.paths.generated
    generated_image_extension: str = config.render.output_extension
    generated_image_jpeg_quality: int = Field(default=config.render.jpeg_quality, ge=1, le=PERCENT_MAX)
    telegram_max_document_size_bytes: int = Field(default=config.render.max_output_bytes, gt=0)
    generation_estimated_jpeg_bytes_per_pixel: float = Field(default=config.render.estimated_jpeg_bytes_per_pixel, gt=0, allow_inf_nan=False)
    rabbitmq_prefetch_count: int = Field(default=config.legacy.rabbitmq_prefetch_count, ge=1, le=8)
    api_queue_maxsize: int = Field(default=config.legacy.api_queue_maxsize, ge=1, le=100)
    api_request_timeout_seconds: float = Field(default=config.legacy.api_request_timeout_seconds, gt=0, le=3600)
    api_shutdown_timeout_seconds: float = Field(default=config.legacy.api_shutdown_timeout_seconds, gt=0, le=300)
    rabbitmq_max_retries: int = Field(default=config.legacy.rabbitmq_max_retries, ge=0, le=5)
    rpc_result_ttl_seconds: int = Field(default=config.legacy.rpc_result_ttl_seconds, ge=60, le=604800)
    rpc_result_max_entries: int = Field(default=config.legacy.rpc_result_max_entries, ge=1, le=10000)

    # Image validation settings
    allowed_formats: List[str] = list(config.image.allowed_color_modes)
    min_size: Tuple[int, int] = (config.image.min_width, config.image.min_height)
    max_size: Tuple[int, int] = (config.image.max_width, config.image.max_height)
    aspect_ratio_limits: Tuple[float, float] = (config.image.aspect_ratio_min, config.image.aspect_ratio_max)

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
        if any(value > limit for value, limit in zip(
                self.max_size, (config.limits.decoder_width, config.limits.decoder_height))):
            raise ValueError('Input image dimensions must fit the 4000 pixel decoder safety ceiling')
        low, high = self.aspect_ratio_limits
        if not (math.isfinite(low) and math.isfinite(high) and 0 < low <= high):
            raise ValueError('Aspect ratio limits must be finite, positive and ordered')
        if self.generated_image_extension.lower().lstrip('.') not in OUTPUT_EXTENSIONS:
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
