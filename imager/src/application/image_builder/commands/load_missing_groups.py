from src.infrastructure.data.image_builder.unit_of_work import get_uow
from src.shared_kernel.result import Result

from ..interfaces_cqrs import ICommand, ICommandHandler


class LoadMissingGroupsCommand(ICommand):
    pass


class LoadMissingGroupsCommandHandler(ICommandHandler):
    def handle(self, command: LoadMissingGroupsCommand) -> Result:
        with get_uow() as uow:
            result = uow.cell_repository.load_missing_groups()
            return result
