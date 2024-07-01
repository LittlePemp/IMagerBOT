import os
from imager.infrastructure.data.image_builder.unit_of_work import get_uow
from imager.shared_kernel.result import Result


class ValidateImagesDataCommand:
    def __init__(self, directory: str):
        self.directory = directory


class ValidateImagesDataCommandHandler:
    def __init__(self):
        self.uow = get_uow()

    def handle(self, command: ValidateImagesDataCommand) -> Result:
        file_repository = self.uow.file_repository
        directory = command.directory

        invalid_data = []
        for group_name in os.listdir(directory):
            group_path = os.path.join(directory, group_name)
            if os.path.isdir(group_path):
                for image_name in os.listdir(group_path):
                    image_path = os.path.join(group_path, image_name)
                    image_data = file_repository.read_image_file(image_path)
                    if not self.validate_image_data(image_data):
                        invalid_data.append(image_path)

        if invalid_data:
            return Result.Error(f'Invalid image data: {invalid_data}')
        return Result.Success('All image data is valid')

    def validate_image_data(self, image_data) -> bool:
        # TODO: validate for all image params
        return True
