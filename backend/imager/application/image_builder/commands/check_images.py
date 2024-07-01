import os
from imager.infrastructure.data.image_builder.unit_of_work import get_uow
from imager.shared_kernel.result import Result


class CheckImagesCommand:
    def __init__(self, directory: str):
        self.directory = directory


class CheckImagesCommandHandler:
    def __init__(self):
        self.uow = get_uow()

    def handle(self, command: CheckImagesCommand) -> Result:
        file_repository = self.uow.file_repository
        directory = command.directory

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
