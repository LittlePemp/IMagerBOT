from src.infrastructure.data.image_builder.unit_of_work import get_uow

from ..interfaces_cqrs import IQuery, IQueryHandler


class GetImageDataQuery(IQuery):
    def __init__(self, image_path: str):
        self.image_path = image_path


class GetImageDataQueryHandler(IQueryHandler):
    def __init__(self):
        self.uow = get_uow()

    def handle(self, query: GetImageDataQuery):
        file_repository = self.uow.file_repository
        return file_repository.read_image_file(query.image_path)