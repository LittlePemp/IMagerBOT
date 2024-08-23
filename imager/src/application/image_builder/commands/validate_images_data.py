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
        try:
            with get_uow() as uow:
                file_repository = uow.file_repository
                cell_repository = uow.cell_repository

                image_files = self._get_image_files(file_repository, command.group_name)
                db_image_files = self._get_db_image_files(cell_repository)

                missing_in_db = image_files - db_image_files
                if missing_in_db:
                    return CommandsErrorMessages.images_missing_in_database(missing_in_db)

                missing_on_disk = db_image_files - image_files
                if missing_on_disk:
                    return CommandsErrorMessages.images_missing_on_disk(missing_on_disk)

                return Result.Success(f'All images in {command.group_name} are validated successfully')
        except Exception as e:
            return CommandsErrorMessages.general_error(e)

    def _get_image_files(self, file_repository, group_name: str) -> set:
        directory = os.path.join(settings.image_groups_relative_path, group_name)
        return set(file_repository.list_image_files(directory))

    def _get_db_image_files(self, cell_repository) -> set:
        return set(
            cell.relative_file_path
            for cell in cell_repository.mongo_repository.filter()
        )
