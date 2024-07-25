import os
from zoneinfo import ZoneInfo

from pydantic import SecretStr
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    api_token: SecretStr
    mongodb_uri: str
    imager_service_url: str
    telegram_admin_ids: str  # List read fix
    database_name: str = 'telegram_bot'
    image_groups_relative_path: str = 'files/groups'
    uploaded_images_path: str = 'files/uploaded'
    generated_images_path: str = 'files/generated'
    tzinfo: ZoneInfo = ZoneInfo('Europe/Moscow')

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
