import os
from collections import defaultdict
from imager.domain.image_builder.aggregates.cell import Cell
from imager.domain.image_builder.value_objects.cell_rgb import CellRgb
from imager.domain.image_builder.repository_interface import IRepository
from imager.infrastructure.data.image_builder.unit_of_work import MongoUnitOfWork  # noqa
from imager.infrastructure.data.image_builder.models.cell_model import CellModel  # noqa
from imager.shared_kernel.loggers import db_logger


class CellRepository(IRepository):
    def __init__(self, uow: MongoUnitOfWork):
        self.uow = uow
        self._data: dict[str, dict[str, Cell]] = defaultdict(dict)

    def initialize(self):
        self.__connect_to_database()
        self.__process_image_groups()

    def __connect_to_database(self):
        self.db = self.uow.repository.db
        db_logger.info('MONGO CELLS CONNECTED')

    def __process_image_groups(self):
        base_path = self.uow.settings.image_groups_relative_path
        for group_name in os.listdir(base_path):
            group_path = os.path.join(base_path, group_name)
            self.__read_all_cells_by_group(group_name)
            self.__process_images_in_group(group_path, group_name)

    def __read_all_cells_by_group(self, group: str) -> None:
        cell_models = self.db[CellModel.AssimilatorConfig.collection].find(
            {'group': group}
        )

        if group not in self._data:
            self._data[group] = {}

        for config in cell_models:
            cell = Cell(
                CellRgb.create(config['r'], config['g'], config['b']).value,
                config['group'],
                config['relative_file_path']
            )
            self._data[group][config['relative_file_path']] = cell

    def __process_images_in_group(self, group_path: str, group_name: str):
        for image_name in os.listdir(group_path):
            image_path = os.path.join(group_path, image_name)
            self.__process_single_image(image_path, group_name)

    def __process_single_image(self, image_path: str, group_name: str):
        existing_config = self.db[
            CellModel.AssimilatorConfig.collection].find_one(
                {'relative_file_path': image_path}
        )
        if not existing_config:
            if self.__create_and_save_cell(image_path, group_name):
                db_logger.info(f'SAVED {os.path.basename(image_path)}')

    def __create_and_save_cell(self, image_path: str, group_name: str) -> bool:
        image_result = self.uow.image_service.read_image(image_path)
        if not image_result.is_success:
            return False
        image = image_result.value

        avg_color_tuple = self.uow.image_service.average_color(image)
        avg_color_result = CellRgb.create(*avg_color_tuple)

        if not avg_color_result.is_success:
            self.uow.image_service.delete_image(image_path)
            return False

        cell_result = Cell.create(avg_color_result.value, group_name, image_path)  # noqa
        if not cell_result.is_success:
            self.uow.image_service.delete_image(image_path)
            return False

        cell: Cell = cell_result.value
        new_config = {
            'r': cell.rgb.r.value,
            'g': cell.rgb.g.value,
            'b': cell.rgb.b.value,
            'group': cell.group,
            'relative_file_path': cell.relative_file_path
        }
        self.db[CellModel.AssimilatorConfig.collection].insert_one(new_config)

        db_logger.info(f'CREATED {cell}')

        if group_name not in self._data:
            self._data[group_name] = {}
        self._data[group_name][image_path] = cell
        return True

    @property
    def data(self):
        return self._data
