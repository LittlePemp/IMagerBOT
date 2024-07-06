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

    @patch('src.infrastructure.data.image_builder.unit_of_work.MongoClient')
    def test_initialization(self, mock_mongo_client):
        mock_client = MagicMock(spec=MongoClient)
        mock_mongo_client.return_value = mock_client

        uow = MongoUnitOfWork()

        mock_mongo_client.assert_called_with(settings.mongodb_uri)

        self.assertIsInstance(uow.cell_repository, CellRepository)
        self.assertIsInstance(uow.file_repository, FileRepository)

    @patch('pymongo.MongoClient')
    def test_initialize_repositories(self, mock_mongo_client):
        mock_client = MagicMock(spec=MongoClient)
        mock_mongo_client.return_value = mock_client

        uow = MongoUnitOfWork()
        uow.cell_repository = MagicMock()
        uow.initialize_repositories()

        uow.cell_repository.initialize.assert_called_once()

    @patch('pymongo.MongoClient')
    def test_get_uow(self, mock_mongo_client):
        mock_client = MagicMock(spec=MongoClient)
        mock_mongo_client.return_value = mock_client

        with patch('src.infrastructure.data.image_builder.unit_of_work.MongoUnitOfWork.initialize_repositories') as mock_initialize:
            uow = get_uow()

            self.assertIsInstance(uow, MongoUnitOfWork)
            mock_initialize.assert_called_once()
