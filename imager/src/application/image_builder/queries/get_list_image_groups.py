from src.application.image_builder.interfaces_cqrs import IQueryHandler, IQuery
from src.infrastructure.data.image_builder.unit_of_work import get_uow


class GetListImageGroupsQuery(IQuery):
    pass


class GetListImageGroupsQueryHandler(IQueryHandler):
    def handle(self, query: GetListImageGroupsQuery) -> list:
        return get_uow().cell_repository.get_all_groups_info()
