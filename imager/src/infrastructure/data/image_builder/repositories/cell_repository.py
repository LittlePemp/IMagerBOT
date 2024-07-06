import os
from collections import defaultdict

from assimilator.mongo.database import MongoRepository
from src.domain.image_builder.aggregates.cell import Cell
from src.domain.image_builder.entities.cell_object import CellObject
from src.domain.image_builder.repository_interface import IRepository
from src.domain.image_builder.services.kdtree_service import KDTreeService
from src.domain.image_builder.value_objects.cell_rgb import CellRgb
from src.shared_kernel.loggers import db_logger
from src.shared_kernel.result import Result

from ..models.cell_model import CellModel
from .file_repository import FileRepository


class CellRepository(IRepository):
    _instance = None

    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            cls._instance = super(CellRepository, cls).__new__(cls)
        return cls._instance

    def __init__(self, mongo_repository: MongoRepository,
                 file_repository: FileRepository):
        if not hasattr(self, '_initialized'):
            self.mongo_repository = mongo_repository
            self.file_repository = file_repository
            self._data = defaultdict(dict)
            self._trees = {}
            self._initialized = False

    def initialize(self):
        if not self._initialized:
            self.load_data_from_mongo()
            self.load_images()
            self.__build_trees()
            db_logger.info('CellRepository initialized')
            self._initialized = True

    def load_data_from_mongo(self):
        for group in self.get_all_groups():
            cell_models = self.mongo_repository.filter(
                self.mongo_repository.specs.filter(group=group))
            for config in cell_models:
                cell_rgb = CellRgb.create(config.r, config.g, config.b)
                if cell_rgb.is_success:
                    cell = Cell(cell_rgb.value,
                                config.group,
                                config.relative_file_path)
                    cell_object_result = CellObject.create(cell)
                    if cell_object_result.is_success:
                        self._data[
                            group
                        ][
                            config.relative_file_path
                        ] = cell_object_result.value

    def load_images(self):
        for _, cell_dict in self._data.items():
            for _, cell_obj in cell_dict.items():
                image_result = self.file_repository.read_image_file(
                    cell_obj.cell.relative_file_path)
                if image_result.is_success:
                    cell_obj.image = image_result.value

    def find_closest_cell(self,
                          pixel_rgb: tuple[int, int, int],
                          group_name: str) -> CellObject:
        tree, cell_objects = self._trees.get(group_name, (None, None))
        if tree is None:
            return None
        _, idx = KDTreeService.find_closest(tree, pixel_rgb)
        return cell_objects[idx] if idx < len(cell_objects) else None

    def load_missing_groups(self) -> Result:
        base_path = self.file_repository.settings.image_groups_relative_path
        all_groups = set(os.listdir(base_path))
        existing_groups = set(self.get_all_groups())
        missing_groups = all_groups - existing_groups

        for group_name in missing_groups:
            group_path = os.path.join(base_path, group_name)
            image_files = self.file_repository.list_image_files(group_path)
            for image_file in image_files:
                image_result = self.file_repository.read_image_file(image_file)
                if not image_result.is_success:
                    return Result.Error(image_result.error)

                image = image_result.value
                avg_color_tuple = self.file_repository.image_service.average_color(image)  # noqa
                avg_color_result = CellRgb.create(*avg_color_tuple)

                if not avg_color_result.is_success:
                    self.file_repository.delete_image_file(image_file)
                    return Result.Error(avg_color_result.error)

                cell_result = CellObject.create_from_image(
                    avg_color_result.value,
                    group_name,
                    image_file)
                if not cell_result.is_success:
                    self.file_repository.delete_image_file(image_file)
                    return Result.Error(cell_result.error)

                cell: CellObject = cell_result.value
                new_config = CellModel(
                    r=cell.cell.rgb.r.value,
                    g=cell.cell.rgb.g.value,
                    b=cell.cell.rgb.b.value,
                    group=cell.cell.group,
                    relative_file_path=cell.cell.relative_file_path
                )
                self.mongo_repository.save(new_config)
                db_logger.info(f'CREATED {cell}')

        return Result.Success('Missing groups loaded successfully')

    def get_all_groups(self) -> list[str]:
        pipeline = [{'$group': {'_id': '$group'}}]
        results = self.mongo_repository._collection.aggregate(pipeline)
        return [result['_id'] for result in results]

    def __build_trees(self):
        self._trees = KDTreeService.build_trees(self._data)

    @property
    def data(self):
        return self._data
