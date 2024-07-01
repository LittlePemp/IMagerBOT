import os
from collections import defaultdict
from imager.shared_kernel.result import Result
from imager.infrastructure.data.image_builder.repositories.file_repository import FileRepository
from imager.domain.image_builder.aggregates.cell import Cell
from imager.domain.image_builder.repository_interface import IRepository
from imager.domain.image_builder.value_objects.cell_rgb import CellRgb
from imager.infrastructure.data.image_builder.models.cell_model import CellModel
from imager.shared_kernel.loggers import db_logger
from assimilator.mongo.database import MongoRepository


class CellRepository(IRepository):
    def __init__(self, mongo_repository: MongoRepository, file_repository: FileRepository):
        self.mongo_repository = mongo_repository
        self.file_repository = file_repository
        self._data: dict[str, dict[str, Cell]] = defaultdict(dict)

    def initialize(self):
        db_logger.info('CellRepository initialized')

    def __process_image_groups(self):
        base_path = self.file_repository.settings.image_groups_relative_path
        for group_name in os.listdir(base_path):
            group_path = os.path.join(base_path, group_name)
            self.__read_all_cells_by_group(group_name)
            self.__process_images_in_group(group_path, group_name)

    def __read_all_cells_by_group(self, group: str) -> None:
        cell_models = self.mongo_repository.filter(
            self.mongo_repository.specs.filter(group=group)
        )

        if group not in self._data:
            self._data[group] = {}

        for config in cell_models:
            cell = Cell(
                CellRgb.create(config.r, config.g, config.b).value,
                config.group,
                config.relative_file_path
            )
            self._data[group][config.relative_file_path] = cell

    def __process_images_in_group(self, group_path: str, group_name: str):
        for image_name in os.listdir(group_path):
            image_path = os.path.join(group_path, image_name)
            self.__process_single_image(image_path, group_name)

    def __process_single_image(self, image_path: str, group_name: str):
        existing_config = self.mongo_repository.filter(
            self.mongo_repository.specs.filter(relative_file_path=image_path),
            lazy=True
        )
        if not existing_config:
            if self.__create_and_save_cell(image_path, group_name):
                db_logger.info(f'SAVED {os.path.basename(image_path)}')

    def __create_and_save_cell(self, image_path: str, group_name: str) -> bool:
        image_result = self.file_repository.read_image_file(image_path)
        if not image_result.is_success:
            return False
        image = image_result.value

        avg_color_tuple = self.file_repository.image_service.average_color(image)
        avg_color_result = CellRgb.create(*avg_color_tuple)

        if not avg_color_result.is_success:
            self.file_repository.delete_image_file(image_path)
            return False

        cell_result = Cell.create(avg_color_result.value, group_name, image_path)
        if not cell_result.is_success:
            self.file_repository.delete_image_file(image_path)
            return False

        cell: Cell = cell_result.value
        new_config = CellModel(
            r=cell.rgb.r.value,
            g=cell.rgb.g.value,
            b=cell.rgb.b.value,
            group=cell.group,
            relative_file_path=cell.relative_file_path
        )
        self.mongo_repository.save(new_config)

        db_logger.info(f'CREATED {cell}')

        if group_name not in self._data:
            self._data[group_name] = {}
        self._data[group_name][image_path] = cell
        return True

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
                avg_color_tuple = self.file_repository.image_service.average_color(image)
                avg_color_result = CellRgb.create(*avg_color_tuple)

                if not avg_color_result.is_success:
                    self.file_repository.delete_image_file(image_file)
                    return Result.Error(avg_color_result.error)

                cell_result = Cell.create(avg_color_result.value, group_name, image_file)
                if not cell_result.is_success:
                    self.file_repository.delete_image_file(image_file)
                    return Result.Error(cell_result.error)

                cell: Cell = cell_result.value
                new_config = CellModel(
                    r=cell.rgb.r.value,
                    g=cell.rgb.g.value,
                    b=cell.rgb.b.value,
                    group=cell.group,
                    relative_file_path=cell.relative_file_path
                )
                self.mongo_repository.save(new_config)
                db_logger.info(f'CREATED {cell}')

        return Result.Success('Missing groups loaded successfully')

    def get_all_groups(self) -> list[str]:
        pipeline = [
            {"$group": {"_id": "$group"}}
        ]
        results = self.mongo_repository._collection.aggregate(pipeline)
        return [result['_id'] for result in results]

    @property
    def data(self):
        return self._data
