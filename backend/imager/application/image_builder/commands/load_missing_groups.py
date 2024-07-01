from imager.infrastructure.data.image_builder.unit_of_work import get_uow
from imager.shared_kernel.result import Result


class LoadMissingGroupsCommand:
    pass


class LoadMissingGroupsCommandHandler:
    def handle(self, command: LoadMissingGroupsCommand) -> Result:
        uow = get_uow()
        try:
            result = uow.cell_repository.load_missing_groups()
            return result
        except Exception as e:
            return Result.Error(f'An error occurred: {e}')
