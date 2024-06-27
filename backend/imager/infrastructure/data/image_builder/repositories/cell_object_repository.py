from collections import defaultdict
from imager.domain.image_builder.entities.cell_object import CellObject
from imager.domain.image_builder.kd_tree.kd_node import KDNode  # noqa
from imager.domain.image_builder.repository_interface import IRepository
from imager.infrastructure.data.image_builder.unit_of_work import MongoUnitOfWork  # noqa
from imager.shared_kernel.loggers import db_logger


class CellObjectRepository(IRepository):
    def __init__(self, uow: MongoUnitOfWork):
        '''
        self._data - хранилище формата
        {
            'group_name1':
            {
                'file_path1': CellObject,
                'file_path2': CellObject
            },
            ...
        }
        '''
        self.uow = uow
        self._data: dict[str, dict[str, CellObject]] = defaultdict(dict)

    def initialize(self):
        for group, cell_infos in self.uow.cell_repository.data.items():
            points = []
            for cell_path, cell in cell_infos.items():
                cell_object_result = CellObject.create(cell)
                if cell_object_result.is_success:
                    rgb = (cell_object_result.value.cell.rgb.r,
                           cell_object_result.value.cell.rgb.g,
                           cell_object_result.value.cell.rgb.b)
                    points.append((rgb, cell_object_result.value))
            self._data[group] = self.uow.kd_tree_service.build_kdtree(points)
        db_logger.info('CellObjectRepository INITED')

    def find_closest_cell(
        self, pixel_rgb: tuple[int, int, int],
        group_name: str
    ) -> CellObject:
        tree = self._data.get(group_name)
        if tree is None:
            return None
        closest_node = self.uow.kd_tree_service.find_closest(tree, pixel_rgb)
        return closest_node.value if closest_node else None

    @property
    def data(self):
        return self._data
