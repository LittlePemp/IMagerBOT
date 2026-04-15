import os
from collections import defaultdict

from pymongo.collection import Collection
from src.domain.image_builder.aggregates.cell import Cell
from src.domain.image_builder.entities.cell_object import CellObject
from src.domain.image_builder.repository_interface import IRepository
from src.domain.image_builder.services.kdtree_service import KDTreeService
from src.domain.image_builder.value_objects.cell_rgb import CellRgb
from src.shared_kernel.loggers import db_logger
from src.shared_kernel.result import Result

from .file_repository import FileRepository


class CellRepository(IRepository):
    _instance = None

    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            cls._instance = super(CellRepository, cls).__new__(cls)
        return cls._instance

    def __init__(self, mongo_collection: Collection,
                 file_repository: FileRepository):
        if not hasattr(self, '_initialized'):
            self.mongo_collection = mongo_collection
            self.file_repository = file_repository
            self._data = defaultdict(dict)
            self._trees = {}
            self._initialized = False

    def initialize(self):
        if not self._initialized:
            self.refresh()
            db_logger.info('CellRepository initialized')
            self._initialized = True

    def refresh(self):
        self._data = defaultdict(dict)
        self._trees = {}
        self.load_data_from_mongo()
        self.load_images()
        self.__build_trees()

    def load_data_from_mongo(self):
        for group in self.get_all_groups():
            cell_models = self.mongo_collection.find({'group': group})
            for config in cell_models:
                stored_path = config['relative_file_path']
                resolved_path = self.file_repository.resolve_image_path(stored_path)
                if resolved_path != os.path.normpath(stored_path):
                    self.mongo_collection.update_one(
                        {'_id': config['_id']},
                        {'$set': {'relative_file_path': resolved_path}}
                    )

                cell_rgb = CellRgb.create(config['r'], config['g'], config['b'])
                if cell_rgb.is_success:
                    cell = Cell(cell_rgb.value, config['group'], resolved_path)
                    cell_object_result = CellObject.create(cell)
                    if cell_object_result.is_success:
                        self._data[group][resolved_path] = cell_object_result.value

    def load_images(self):
        for _, cell_dict in self._data.items():
            for _, cell_obj in cell_dict.items():
                image_result = self.file_repository.read_image_file(cell_obj.cell.relative_file_path)
                if image_result.is_success:
                    cell_obj.image = image_result.value

    def find_closest_cell(self, pixel_rgb: tuple[int, int, int], group_name: str) -> CellObject:
        tree, cell_objects = self._trees.get(group_name, (None, None))
        if tree is None:
            return None
        closest_result = KDTreeService.find_closest(tree, pixel_rgb)
        if closest_result:
            _, idx = closest_result.value
        return cell_objects[idx] if idx < len(cell_objects) else None

    def load_missing_groups(self, group_names: list[str] | None = None) -> Result:
        try:
            base_path = self.file_repository.settings.image_groups_relative_path
            all_groups = self._resolve_target_groups(base_path, group_names)
            existing_file_paths = self._get_existing_file_paths()
            created_count = 0

            for group_name in all_groups:
                group_path = os.path.join(base_path, group_name)
                image_files_result = self.file_repository.list_image_files(group_path)
                if not image_files_result.is_success:
                    return Result.Error(image_files_result.error)

                for image_file in image_files_result.value:
                    if image_file in existing_file_paths:
                        continue

                    add_result = self._add_image_to_collection(group_name, image_file)
                    if not add_result.is_success:
                        return add_result
                    existing_file_paths.add(image_file)
                    created_count += 1

            if created_count:
                self.refresh()

            return Result.Success(f'Synchronized {created_count} images')
        except Exception as e:
            return Result.Error(f'Failed to synchronize image groups: {e}')

    def _resolve_target_groups(self, base_path: str, group_names: list[str] | None) -> list[str]:
        if group_names:
            return [group_name for group_name in group_names if os.path.isdir(os.path.join(base_path, group_name))]
        return [
            group_name
            for group_name in os.listdir(base_path)
            if os.path.isdir(os.path.join(base_path, group_name))
        ]

    def _get_existing_file_paths(self) -> set[str]:
        return {
            item['relative_file_path']
            for item in self.mongo_collection.find({}, {'relative_file_path': 1})
        }

    def _add_image_to_collection(self, group_name: str, image_file: str) -> Result:
        image_result = self.file_repository.read_image_file(image_file)
        if not image_result.is_success:
            return Result.Error(image_result.error)

        image = image_result.value
        if len(image.shape) == 3 and image.shape[2] == 4:
            convert_result = self.file_repository.image_service.convert_rgba_to_rgb(image)
            if not convert_result.is_success:
                return Result.Error(convert_result.error)
            image = convert_result.value

        avg_color_result = self.file_repository.image_service.average_color(image)
        if not avg_color_result.is_success:
            return Result.Error(avg_color_result.error)

        avg_rgb = avg_color_result.value[:3]
        cell_rgb_result = CellRgb.create(*avg_rgb)
        if not cell_rgb_result.is_success:
            return Result.Error(cell_rgb_result.error)

        cell_result = CellObject.create_from_image(
            cell_rgb_result.value,
            group_name,
            image_file,
        )
        if not cell_result.is_success:
            return Result.Error(cell_result.error)

        cell: CellObject = cell_result.value
        new_config = {
            'r': cell.cell.rgb.r.value,
            'g': cell.cell.rgb.g.value,
            'b': cell.cell.rgb.b.value,
            'group': cell.cell.group,
            'relative_file_path': cell.cell.relative_file_path
        }
        self.mongo_collection.insert_one(new_config)
        db_logger.info(f'CREATED {cell}')
        return Result.Success(new_config)

    def get_all_groups(self) -> list[str]:
        pipeline = [{'$group': {'_id': '$group'}}]
        results = self.mongo_collection.aggregate(pipeline)
        return [result['_id'] for result in results]

    def get_all_groups_info(self) -> list[dict]:
        pipeline = [
            {'$group': {'_id': '$group', 'count': {'$sum': 1}}},
            {'$project': {'_id': 0, 'group': '$_id', 'count': 1}}
        ]
        results = list(self.mongo_collection.aggregate(pipeline))

        return results

    def __build_trees(self):
        self._trees = KDTreeService.build_trees(self._data).value

    @property
    def data(self):
        return self._data
