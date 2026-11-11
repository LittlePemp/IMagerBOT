import os

from settings import settings
from src.application.image_builder.interfaces_cqrs import ICommand, ICommandHandler
from src.infrastructure.data.image_builder.unit_of_work import get_uow
from src.shared_kernel.loggers import application_logger
from src.shared_kernel.result import Result


class ValidateImagesDataCommand(ICommand):
    def __init__(self, group_name):
        self.group_name = group_name


class ValidateImagesDataCommandHandler(ICommandHandler):
    @staticmethod
    def _path_key(path):
        return os.path.normcase(os.path.realpath(path))

    def handle(self, command: ValidateImagesDataCommand) -> Result:
        try:
            if not isinstance(command.group_name, str) or not command.group_name.strip():
                raise ValueError('Image group must be a non-empty string')
            root = os.path.realpath(settings.image_groups_relative_path)
            directory = os.path.realpath(os.path.join(root, command.group_name))
            if directory == root or os.path.commonpath([root, directory]) != root:
                raise ValueError('Image group must be inside the configured library root')
            with get_uow() as uow:
                files = uow.file_repository.list_image_files(directory)
                if not files.is_success:
                    application_logger.error(str(files.error))
                    return files
                disk_paths = {self._path_key(path) for path in files.value}
                records = uow.cell_repository.mongo_collection.find(
                    {'group': command.group_name}, {'relative_file_path': 1},
                )
                database_paths = {
                    self._path_key(uow.file_repository.resolve_image_path(record['relative_file_path']))
                    for record in records
                }
                missing_in_database = disk_paths - database_paths
                missing_on_disk = database_paths - disk_paths
                if missing_in_database or missing_on_disk:
                    message = (f'Catalog mismatch for {command.group_name}: '
                               f'missing_in_database={sorted(missing_in_database)}; '
                               f'missing_on_disk={sorted(missing_on_disk)}')
                    application_logger.error(message)
                    return Result.Error(message)
            return Result.Success(f'Files and database agree for group {command.group_name}')
        except Exception as exc:
            application_logger.exception(f'Cannot validate catalog: {exc}')
            return Result.Error(f'Cannot validate catalog: {exc}')
