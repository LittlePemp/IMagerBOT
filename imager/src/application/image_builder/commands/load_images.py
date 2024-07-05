import os
from src.infrastructure.data.image_builder.unit_of_work import get_uow
from src.shared_kernel.result import Result


class LoadImagesCommand:
    def __init__(self, directory: str):
        self.directory = directory


class LoadImagesCommandHandler:
    def __init__(self):
        self.uow = get_uow()

    def handle(self, command: LoadImagesCommand) -> Result:
        file_repository = self.uow.file_repository
        directory = command.directory

        for group_name in os.listdir(directory):
            group_path = os.path.join(directory, group_name)
            if os.path.isdir(group_path):
                for image_name in os.listdir(group_path):
                    image_path = os.path.join(group_path, image_name)
                    file_repository.load_image(image_path, group_name)

        return Result.Success('Images loaded successfully')
