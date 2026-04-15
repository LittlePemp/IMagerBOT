import unittest
from unittest.mock import MagicMock, patch

from src.application.image_builder.commands.download_topic import \
    DownloadTopicCommand, DownloadTopicCommandHandler
from src.shared_kernel.result import Result


class TestDownloadTopicCommandHandler(unittest.TestCase):
    def setUp(self):
        self.command = DownloadTopicCommand(
            keyword='cats',
            group_name='cats_group',
            limit=10,
            max_pages=3,
            sync_to_db=True,
        )

    def test_handle_success(self):
        downloader = MagicMock()
        downloader.download_topic.return_value = Result.Success(
            MagicMock(to_dict=lambda: {
                'keyword': 'cats',
                'group_name': 'cats_group',
                'downloaded_count': 5,
                'skipped_existing_count': 1,
                'discovered_count': 6,
                'directory': 'files/groups/cats_group',
            }, group_name='cats_group')
        )
        handler = DownloadTopicCommandHandler(downloader=downloader)

        mock_uow = MagicMock()
        mock_uow.__enter__.return_value = mock_uow
        mock_uow.__exit__.return_value = None
        mock_uow.cell_repository.load_missing_groups.return_value = Result.Success('Synchronized 5 images')

        with patch('src.application.image_builder.commands.download_topic.get_uow', return_value=mock_uow):
            result = handler.handle(self.command)

        self.assertTrue(result.is_success)
        self.assertEqual(result.value['group_name'], 'cats_group')
        mock_uow.cell_repository.load_missing_groups.assert_called_once_with(group_names=['cats_group'])

    def test_handle_download_error(self):
        downloader = MagicMock()
        downloader.download_topic.return_value = Result.Error('network error')
        handler = DownloadTopicCommandHandler(downloader=downloader)

        result = handler.handle(self.command)

        self.assertFalse(result.is_success)
        self.assertEqual(result.error, 'network error')
