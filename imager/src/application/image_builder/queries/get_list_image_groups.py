from src.infrastructure.data.image_builder.unit_of_work import get_uow


class GetListImageGroupsQuery:
    pass


class GetListImageGroupsQueryHandler:
    def __init__(self):
        self.uow = get_uow()

    def handle(self, query: GetListImageGroupsQuery) -> list:
        return self.uow.cell_repository.get_all_groups()
