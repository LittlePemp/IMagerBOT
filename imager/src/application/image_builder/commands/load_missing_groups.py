from src.infrastructure.data.image_builder.unit_of_work import get_uow
from src.shared_kernel.result import Result

from ..errors.commands_errors import CommandsErrorMessages
from ..interfaces_cqrs import ICommand, ICommandHandler


class LoadMissingGroupsCommand(ICommand):
    pass


class LoadMissingGroupsCommandHandler(ICommandHandler):
    def handle(self, command: LoadMissingGroupsCommand) -> Result:
        uow = get_uow()
        try:
            result = uow.cell_repository.load_missing_groups()
            return result
        except Exception as e:
            return CommandsErrorMessages.general_error(e)
