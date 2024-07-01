import os
from dotenv import load_dotenv

load_dotenv()


class Settings:
    def __init__(self):
        self.mongodb_uri = os.getenv('MONGODB_URI')
        self.database_name = os.getenv('DATABASE_NAME')
        self.image_groups_relative_path = os.getenv(
            'IMAGE_GROUPS_RELATIVE_PATH',
            'files/groups')
        self.uploaded_images_path = os.getenv(
            'UPLOADED_IMAGES_PATH',
            'files/uploaded')
        self.generated_images_path = os.getenv(
            'GENERATED_IMAGES_PATH',
            'files/generated')

        self.create_directories()

    def create_directories(self):
        os.makedirs(self.image_groups_relative_path, exist_ok=True)
        os.makedirs(self.uploaded_images_path, exist_ok=True)
        os.makedirs(self.generated_images_path, exist_ok=True)


settings = Settings()
