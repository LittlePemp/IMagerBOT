import os
from zoneinfo import ZoneInfo

from pydantic import SecretStr, Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    api_token: SecretStr
    mongodb_uri: str
    rabbitmq_url: str = 'amqp://guest:guest@127.0.0.1:5672/'
    rabbitmq_requests_queue: str = 'imager.requests'
    telegram_admin_ids: str  # List read fix
    database_name: str = 'telegram_bot'
    imager_database_name: str = 'imager_core'
    minio_endpoint: str = 'minio:9000'
    minio_access_key: SecretStr = SecretStr('')
    minio_secret_key: SecretStr = SecretStr('')
    minio_bucket: str = 'imager'
    minio_secure: bool = False
    bot_notification_poll_seconds: float = Field(default=2, gt=0, le=60)
    bot_notification_lease_seconds: int = Field(default=120, ge=10, le=3600)
    bot_notification_max_attempts: int = Field(default=5, ge=1, le=20)
    image_groups_relative_path: str = 'files/groups'
    uploaded_images_path: str = 'files/uploaded'
    generated_images_path: str = 'files/generated'
    tzinfo: ZoneInfo = ZoneInfo('Europe/Moscow')
    generation_default_insertion_format: str = 'crop'
    generation_default_alpha_channel: int = 30
    generation_default_noise_level: int = 15
    generation_default_cell_size: int = 40
    generation_default_result_size: int = 120
    generation_noise_level_min: int = 0
    generation_noise_level_max: int = 100
    generation_cell_size_min: int = 1
    generation_cell_size_max: int = 200
    generation_result_size_min: int = 1
    generation_result_size_max: int = 300
    telegram_max_document_size_bytes: int = 47185920
    generation_estimated_jpeg_bytes_per_pixel: float = 1.0
    regular_user_upload_limit: int = 5

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.telegram_admin_ids = [int(x) for x in self.telegram_admin_ids.split('|')]

    class Config:
        env_file = '.env'
        env_file_encoding = 'utf-8'

    def create_directories(self):
        os.makedirs(self.image_groups_relative_path, exist_ok=True)
        os.makedirs(self.uploaded_images_path, exist_ok=True)
        os.makedirs(self.generated_images_path, exist_ok=True)

settings = Settings()
