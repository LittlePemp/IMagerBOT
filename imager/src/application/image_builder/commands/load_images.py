import os

from settings import settings
from src.infrastructure.data.image_builder.unit_of_work import get_uow
from src.shared_kernel.loggers import application_logger
from src.shared_kernel.result import Result

from ..interfaces_cqrs import ICommand, ICommandHandler


class LoadImagesCommand(ICommand):
    def __init__(self, directory: str):
        self.directory = directory


class LoadImagesCommandHandler(ICommandHandler):
    def handle(self, command: LoadImagesCommand) -> Result:
        try:
            root = os.path.realpath(settings.image_groups_relative_path)
            directory = os.path.realpath(command.directory)
            if directory == root:
                groups = None
            elif os.path.dirname(directory) == root and os.path.isdir(directory):
                groups = [os.path.basename(directory)]
            else:
                raise ValueError('Load directory must be the configured library root or one of its groups')
            with get_uow() as uow:
                result = uow.cell_repository.load_missing_groups(groups)
            if not result.is_success:
                application_logger.error(str(result.error))
            return result
        except Exception as exc:
            application_logger.exception(f'Cannot import library: {exc}')
            return Result.Error(f'Cannot import library: {exc}')
