import os

from settings import settings
from src.application.image_builder.interfaces_cqrs import (ICommand,
                                                           ICommandHandler)
from src.infrastructure.data.image_builder.unit_of_work import get_uow
from src.shared_kernel.result import Result

from ..errors.commands_errors import CommandsErrorMessages


class ValidateImagesDataCommand(ICommand):
    def __init__(self, group_name):
        self.group_name = group_name


class ValidateImagesDataCommandHandler(ICommandHandler):
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

            db_image_files = set(
                cell.relative_file_path
                for cell
                in cell_repository.mongo_repository.filter()
            )

            missing_in_db = image_files - db_image_files
            if missing_in_db:
                return CommandsErrorMessages.images_missing_in_database(
                    missing_in_db)

            missing_on_disk = db_image_files - image_files
            if missing_on_disk:
                return CommandsErrorMessages.images_missing_on_disk(
                    missing_on_disk)

            return Result.Success(f'All images in {command.group_name} are '
                                  'validated successfully')
        except Exception as e:
            return CommandsErrorMessages.general_error(e)
