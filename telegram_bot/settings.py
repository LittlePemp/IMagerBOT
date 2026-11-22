import os
from zoneinfo import ZoneInfo
from typing import Annotated
from urllib.parse import urlsplit

from pydantic import SecretStr, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict, NoDecode

from imager_config.loader import config


class Settings(BaseSettings):
    api_token: SecretStr
    support_bot_token: SecretStr | None = None
    telegram_api_base_url: str = config.bot.api_base_url
    telegram_api_local: bool = config.bot.api_local
    mongodb_uri: str
    rabbitmq_url: str = config.legacy.rabbitmq_url
    rabbitmq_requests_queue: str = config.legacy.rabbitmq_requests_queue
    telegram_admin_ids: Annotated[tuple[int, ...], NoDecode]
    model_config = SettingsConfigDict(
        env_file='.env', env_file_encoding='utf-8', extra='ignore', hide_input_in_errors=True,
    )
    database_name: str = config.mongo.bot_database
    imager_database_name: str = config.mongo.core_database
    minio_endpoint: str = config.storage.endpoint
    minio_access_key: SecretStr = SecretStr('')
    minio_secret_key: SecretStr = SecretStr('')
    minio_bucket: str = config.storage.bucket
    minio_secure: bool = config.storage.secure
    bot_notification_poll_seconds: float = Field(default=config.bot.notification_poll_seconds, gt=0)
    bot_notification_lease_seconds: int = Field(default=config.bot.notification_lease_seconds, gt=0)
    image_groups_relative_path: str = config.paths.image_groups
    uploaded_images_path: str = config.paths.uploads
    generated_images_path: str = config.paths.generated
    tzinfo: ZoneInfo = ZoneInfo(config.bot.timezone)
    generation_default_insertion_format: str = config.generation.insertion_format
    generation_default_alpha_channel: int = config.generation.alpha_channel
    generation_default_noise_level: int = config.generation.noise_level
    generation_default_cell_size: int = config.generation.cell_size
    generation_default_result_size: int = config.generation.result_size
    generation_noise_level_min: int = config.bot.noise_min
    generation_noise_level_max: int = config.bot.noise_max
    generation_cell_size_min: int = config.bot.cell_size_min
    generation_cell_size_max: int = config.bot.cell_size_max
    generation_result_size_min: int = config.bot.result_size_min
    generation_result_size_max: int = config.bot.result_size_max
    telegram_max_document_size_bytes: int = config.render.max_output_bytes
    generation_estimated_jpeg_bytes_per_pixel: float = config.render.estimated_jpeg_bytes_per_pixel
    regular_user_upload_limit: int = config.bot.regular_user_upload_limit

    @field_validator('support_bot_token', mode='before')
    @classmethod
    def optional_support_token(cls, value):
        if isinstance(value, str):
            return value.strip() or None
        return value

    @model_validator(mode='after')
    def validate_separate_support_identity(self):
        if (self.support_bot_token is not None
                and self.support_bot_token.get_secret_value() == self.api_token.get_secret_value()):
            raise ValueError('Support must use a different bot token')
        return self

    @field_validator('telegram_admin_ids', mode='before')
    @classmethod
    def parse_fixed_administrators(cls, value):
        values = value.split('|') if isinstance(value, str) else value
        result = tuple(dict.fromkeys(int(item) for item in values))
        if not result or any(item <= 0 for item in result):
            raise ValueError('Configure at least one positive permanent administrator ID')
        return result

    @field_validator('telegram_api_base_url')
    @classmethod
    def validate_api_endpoint(cls, value):
        parsed = urlsplit(value)
        if (parsed.scheme not in ('http', 'https') or not parsed.hostname
                or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise ValueError('Telegram API endpoint must be an HTTP(S) base URL without credentials')
        return value.rstrip('/')

    @model_validator(mode='after')
    def validate_notification_lease(self):
        renew_interval = max(
            config.bot.notification_renew_min_seconds,
            self.bot_notification_lease_seconds / config.bot.notification_renew_divisor,
        )
        if renew_interval >= self.bot_notification_lease_seconds:
            raise ValueError('Notification renewal interval must fit inside its lease')
        return self

    def create_directories(self):
        os.makedirs(self.image_groups_relative_path, exist_ok=True)
        os.makedirs(self.uploaded_images_path, exist_ok=True)
        os.makedirs(self.generated_images_path, exist_ok=True)

settings = Settings()
