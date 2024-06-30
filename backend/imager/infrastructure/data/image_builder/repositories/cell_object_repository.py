from collections import defaultdict
from scipy.spatial import KDTree

from imager.domain.image_builder.entities.cell_object import CellObject
from imager.domain.image_builder.repository_interface import IRepository
from assimilator.mongo.database import MongoRepository
from imager.shared_kernel.loggers import db_logger


class CellObjectRepository(IRepository):
    def __init__(self, mongo_repository: MongoRepository):
        self.mongo_repository = mongo_repository
        self._data = defaultdict(dict)
        self._trees = {}

    def initialize(self):
        self.__build_trees()
        db_logger.info('CellObjectRepository initialized')

    def __build_trees(self):
        for group, cell_infos in self._data.items():
            points = []
            cell_objects = []
            for cell_path, cell in cell_infos.items():
                cell_object_result = CellObject.create(cell)
                if cell_object_result.is_success:
                    rgb = (cell_object_result.value.cell.rgb.r,
                           cell_object_result.value.cell.rgb.g,
                           cell_object_result.value.cell.rgb.b)
                    points.append(rgb)
                    cell_objects.append(cell_object_result.value)
            self._trees[group] = (KDTree(points), cell_objects)

    def find_closest_cell(self, pixel_rgb: tuple[int, int, int], group_name: str) -> CellObject:
        tree, cell_objects = self._trees.get(group_name, (None, None))
        if tree is None:
            return None
        _, idx = tree.query(pixel_rgb)
        return cell_objects[idx] if idx < len(cell_objects) else None

    @property
    def data(self):
        return self._data

    def load_data(self):
        cell_object_models = self.mongo_repository.filter()
        for model in cell_object_models:
            cell = CellObject(
                cell=model.cell,
                relative_file_path=model.relative_file_path,
            )
            self._data[model.group][model.relative_file_path] = cell
        self.__build_trees()
