import unittest
from unittest.mock import MagicMock, patch

from settings import settings
from src.infrastructure.data.image_builder.repositories.cell_repository import \
    CellRepository
from src.infrastructure.data.image_builder.repositories.file_repository import \
    FileRepository
from src.infrastructure.data.image_builder.unit_of_work import (
    MongoClient, MongoUnitOfWork, get_uow)


class TestMongoUnitOfWork(unittest.TestCase):

    def setUp(self):
        MongoUnitOfWork._instance = None

    @patch('src.infrastructure.data.image_builder.unit_of_work.MongoClient')
    def test_singleton_instance(self, mock_mongo_client):
        uow1 = MongoUnitOfWork(client=mock_mongo_client.return_value)
        uow2 = MongoUnitOfWork(client=mock_mongo_client.return_value)
        self.assertIs(uow1, uow2, 'MongoUnitOfWork не является синглтоном')

    @patch('src.infrastructure.data.image_builder.unit_of_work.MongoClient')
    @patch.object(MongoUnitOfWork, '_initialize', MagicMock())
    def test_initialize_called_once(self, mock_mongo_client):
        MongoUnitOfWork(client=mock_mongo_client.return_value)
        MongoUnitOfWork._initialize.assert_called_once()

    @patch('src.infrastructure.data.image_builder.unit_of_work.MongoClient')
    @patch('src.infrastructure.data.image_builder.repositories.cell_repository.CellRepository.__new__')
    def test_repository_initialization(self, mock_cell_repo_new, mock_mongo_client):
        mock_cell_repo_new.return_value = CellRepository.__new__(CellRepository)

        CellRepository._instance = None

        uow = MongoUnitOfWork(client=mock_mongo_client.return_value)

        self.assertTrue(mock_cell_repo_new.called, 'CellRepository не был вызван')

    @patch('src.infrastructure.data.image_builder.unit_of_work.MongoClient')
    def test_session_management(self, mock_mongo_client):
        mock_session = MagicMock()
        mock_mongo_client.return_value.start_session.return_value = mock_session

        with MongoUnitOfWork(client=mock_mongo_client.return_value) as uow:
            mock_session.start_transaction.assert_called_once()

        mock_session.commit_transaction.assert_called_once()
        mock_session.end_session.assert_called_once()

    @patch('src.infrastructure.data.image_builder.unit_of_work.MongoClient')
    def test_initialize_repositories(self, mock_mongo_client):
        mock_repo = MagicMock()
        mock_repo.initialize = MagicMock()
        uow = MongoUnitOfWork(client=mock_mongo_client.return_value)
        uow.repositories_to_init = [mock_repo]

        uow.initialize_repositories()

        mock_repo.initialize.assert_called_once()

    @patch('src.infrastructure.data.image_builder.unit_of_work.MongoClient')
    def test_exit_with_exception(self, mock_mongo_client):
        mock_session = MagicMock()
        mock_mongo_client.return_value.start_session.return_value = mock_session

        try:
            with MongoUnitOfWork(client=mock_mongo_client.return_value):
                raise Exception('Test Exception')
        except Exception:
            pass

        mock_session.abort_transaction.assert_called_once()
        mock_session.end_session.assert_called_once()

    @patch('src.infrastructure.data.image_builder.unit_of_work.MongoClient')
    def test_get_uow(self, mock_mongo_client):
        uow = get_uow()
        self.assertIsInstance(uow, MongoUnitOfWork, 'get_uow не возвращает экземпляр MongoUnitOfWork')
