import os
from imager.infrastructure.data.image_builder.unit_of_work import get_uow
from imager.shared_kernel.result import Result
from settings import settings


class ValidateImagesDataCommand:
    def __init__(self, group_name):
        self.group_name = group_name


class ValidateImagesDataCommandHandler:
    def handle(self, command: ValidateImagesDataCommand) -> Result:
        uow = get_uow()
        try:
            file_repository = uow.file_repository
            cell_repository = uow.cell_repository

            directory = os.path.join(
                settings.image_groups_relative_path,
                command.group_name
            )

            image_files = set(file_repository.list_image_files(directory))

            db_image_files = set(cell.relative_file_path for cell in cell_repository.mongo_repository.filter())

            missing_in_db = image_files - db_image_files
            if missing_in_db:
                return Result.Error(f'Images missing in database: {missing_in_db}')

            missing_on_disk = db_image_files - image_files
            if missing_on_disk:
                return Result.Error(f'Images missing on disk: {missing_on_disk}')

            return Result.Success(f'All images in {command.group_name} are validated successfully')
        except Exception as e:
            return Result.Error(f"An error occurred: {e}")
