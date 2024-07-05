import os

from src.infrastructure.data.image_builder.unit_of_work import get_uow
from src.shared_kernel.result import Result
from settings import settings


class VerifyImagesCommand:
    def __init__(self, group_name: str):
        self.group_name = group_name


class VerifyImagesCommandHandler:
    '''
    TODO: проверка параметров изображения
    '''
    def __init__(self):
        self.uow = get_uow()

    def handle(self, command: VerifyImagesCommand) -> Result:
        file_repository = self.uow.file_repository
        group_name = command.group_name

        directory = os.path.join(settings.image_groups_relative_path, group_name)
        missing_images = []
        for group_name in os.listdir(directory):
            group_path = os.path.join(directory, group_name)
            if os.path.isdir(group_path):
                for image_name in os.listdir(group_path):
                    image_path = os.path.join(group_path, image_name)
                    if not file_repository.image_exists(image_path, group_name):
                        missing_images.append(image_path)

        if missing_images:
            return Result.Error(f'Missing images: {missing_images}')
        return Result.Success('All images are present in the database')
