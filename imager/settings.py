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

        # Image validation settings
        self.allowed_formats = ['RGB', 'RGBA']
        self.min_size = (20, 20)
        self.max_size = (4000, 4000)
        self.aspect_ratio_limits = (9 / 16, 16 / 9)

        self.create_directories()

    def create_directories(self):
        os.makedirs(self.image_groups_relative_path, exist_ok=True)
        os.makedirs(self.uploaded_images_path, exist_ok=True)
        os.makedirs(self.generated_images_path, exist_ok=True)


settings = Settings()
