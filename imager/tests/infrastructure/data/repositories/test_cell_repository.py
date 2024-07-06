import unittest
from collections import defaultdict
from unittest.mock import MagicMock, patch

from src.domain.image_builder.aggregates.cell import Cell
from src.domain.image_builder.entities.cell_object import CellObject
from src.domain.image_builder.value_objects.cell_rgb import (CellRgb,
                                                             ColorDefinition)
from src.infrastructure.data.image_builder.models.cell_model import CellModel
from src.infrastructure.data.image_builder.repositories.cell_repository import \
    CellRepository
from src.shared_kernel.result import Result


class TestCellRepository(unittest.TestCase):
    @patch('src.infrastructure.data.image_builder.repositories.cell_repository.MongoRepository')
    @patch('src.infrastructure.data.image_builder.repositories.cell_repository.FileRepository')
    def setUp(self, mock_mongo_repo, mock_file_repo):
        self.mock_file_repo = mock_file_repo
        self.mock_mongo_repo = mock_mongo_repo
        if CellRepository._instance:
            CellRepository._instance = None
        self.cell_repository = CellRepository(mock_mongo_repo, mock_file_repo)

    def test_initialization(self):
        self.assertFalse(self.cell_repository._initialized)
        self.assertIsInstance(self.cell_repository.mongo_repository, MagicMock)
        self.assertIsInstance(self.cell_repository.file_repository, MagicMock)
        self.assertIsInstance(self.cell_repository._data, defaultdict)
        self.assertEqual(self.cell_repository._trees, {})

    @patch('src.infrastructure.data.image_builder.repositories.cell_repository.CellRepository.load_data_from_mongo')
    @patch('src.infrastructure.data.image_builder.repositories.cell_repository.CellRepository.load_images')
    @patch('src.infrastructure.data.image_builder.repositories.cell_repository.CellRepository._CellRepository__build_trees')
    def test_initialize(self, mock_build_trees,
                        mock_load_images,
                        mock_load_data_from_mongo):
        self.cell_repository.initialize()
        mock_load_data_from_mongo.assert_called_once()
        mock_load_images.assert_called_once()
        mock_build_trees.assert_called_once()
        self.assertTrue(self.cell_repository._initialized)

    @patch('src.domain.image_builder.entities.cell_object.CellObject.create')
    @patch('src.domain.image_builder.value_objects.cell_rgb.CellRgb.create')
    def test_load_data_from_mongo(self,
                                  mock_cell_rgb_create,
                                  mock_cell_object_create):
        mock_cell_rgb_create.side_effect = lambda r, g, b: Result.Success(
            CellRgb(ColorDefinition(r), ColorDefinition(g), ColorDefinition(b)))
        mock_cell_object_create.side_effect = lambda cell: Result.Success(
            CellObject(cell, None))

        self.mock_mongo_repo.filter.return_value = [
            CellModel(r=255, g=0, b=0,
                      group='group1', relative_file_path='path1'),
            CellModel(r=0, g=255, b=0,
                      group='group1', relative_file_path='path2')
        ]

        self.cell_repository.get_all_groups = MagicMock(return_value=['group1'])

        self.cell_repository.load_data_from_mongo()

        print(f"Data loaded: {self.cell_repository._data}")

        self.assertIn('group1', self.cell_repository._data)
        self.assertEqual(len(self.cell_repository._data['group1']), 2)

    @patch('src.domain.image_builder.entities.cell_object.CellObject.create')
    def test_load_images(self, mock_cell_object_create):
        cell_obj = CellObject(
            Cell(
                CellRgb(
                    ColorDefinition(255),
                    ColorDefinition(0),
                    ColorDefinition(0)),
                'group1', 'path1'), None)
        self.cell_repository._data = {'group1': {'path1': cell_obj}}
        self.mock_file_repo.read_image_file.return_value = Result.Success(
            'image_data')
        self.cell_repository.load_images()
        self.assertEqual(self.cell_repository._data['group1']['path1'].image,
                         'image_data')

    @patch('src.domain.image_builder.services.kdtree_service.KDTreeService.find_closest')
    def test_find_closest_cell(self, mock_find_closest):
        tree = MagicMock()
        cell_obj = MagicMock()
        self.cell_repository._trees = {'group1': (tree, [cell_obj])}
        mock_find_closest.return_value = (0, 0)
        result = self.cell_repository.find_closest_cell((255, 0, 0), 'group1')
        self.assertEqual(result, cell_obj)

    @patch('os.listdir')
    @patch('os.path.join')
    @patch('src.domain.image_builder.entities.cell_object.CellObject.create_from_image')
    @patch('src.domain.image_builder.value_objects.cell_rgb.CellRgb.create')
    def test_load_missing_groups(self, mock_cell_rgb_create, mock_cell_object_create, mock_path_join, mock_listdir):
        mock_listdir.return_value = ['group1', 'group2']
        self.cell_repository.get_all_groups = MagicMock(return_value=['group1'])
        self.mock_file_repo.list_image_files.return_value = ['file1', 'file2']
        self.mock_file_repo.read_image_file.return_value = Result.Success('image_data')
        self.mock_file_repo.image_service.average_color.return_value = (255, 0, 0)

        mock_cell_rgb_create.side_effect = lambda r, g, b: Result.Success(
            CellRgb(
                ColorDefinition(r),
                ColorDefinition(g),
                ColorDefinition(b)))
        mock_cell_object_create.side_effect = lambda rgb, group, path: Result.Success(
            CellObject(
                Cell(
                    rgb, group, path), 'image_data'))

        self.cell_repository.mongo_repository.save = MagicMock()

        result = self.cell_repository.load_missing_groups()

        self.assertTrue(result.is_success)
        self.assertEqual(result.value, 'Missing groups loaded successfully')

    def test_get_all_groups(self):
        mock_collection = MagicMock()
        mock_collection.aggregate.return_value = [{'_id': 'group1'},
                                                  {'_id': 'group2'}]
        self.mock_mongo_repo._collection = mock_collection

        result = self.cell_repository.get_all_groups()

        self.assertEqual(result, ['group1', 'group2'])
