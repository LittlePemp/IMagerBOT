import importlib
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import numpy as np
from click.testing import CliRunner
from src.application.image_builder.commands.check_images import CheckImagesCommand, CheckImagesCommandHandler
from src.application.image_builder.commands.load_images import LoadImagesCommand, LoadImagesCommandHandler
from src.application.image_builder.commands.validate_images_data import (
    ValidateImagesDataCommand, ValidateImagesDataCommandHandler,
)
from src.application.image_builder.commands.verify_images import VerifyImagesCommandHandler
from src.application.image_builder.queries.get_image_data import GetImageDataQueryHandler
from src.application.image_builder.queries.get_list_image_groups import GetListImageGroupsQueryHandler
from src.shared_kernel.result import Result


class TestMaintenanceCommands(unittest.TestCase):
    def test_handler_construction_does_not_initialize_database(self):
        with patch('src.infrastructure.data.image_builder.unit_of_work.MongoClient', side_effect=AssertionError('unexpected database access')):
            handlers = [CheckImagesCommandHandler(), LoadImagesCommandHandler(),
                        VerifyImagesCommandHandler(), ValidateImagesDataCommandHandler(),
                        GetImageDataQueryHandler(), GetListImageGroupsQueryHandler()]
        self.assertEqual(len(handlers), 6)

    def test_check_reads_result_value_instead_of_iterating_result(self):
        context = MagicMock()
        uow = context.__enter__.return_value
        uow.file_repository.list_image_files.return_value = Result.Success(['image.png'])
        uow.file_repository.read_image_file.return_value = Result.Success(np.zeros((20, 20, 4), dtype=np.uint8))
        module = 'src.application.image_builder.commands.check_images'
        config = SimpleNamespace(
            image_groups_relative_path='library', min_size=(20, 20), max_size=(4000, 4000),
            allowed_formats=['RGB', 'RGBA'], aspect_ratio_limits=(9 / 16, 16 / 9),
        )
        with patch(module + '.settings', config):
            with patch(module + '.get_uow', return_value=context):
                with patch(module + '.tqdm', side_effect=lambda values, **kwargs: values):
                    result = CheckImagesCommandHandler().handle(CheckImagesCommand('group'))
        self.assertTrue(result.is_success, result.error)

    def test_load_uses_catalog_sync_for_selected_group(self):
        with tempfile.TemporaryDirectory() as root:
            group = os.path.join(root, 'group')
            os.mkdir(group)
            context = MagicMock()
            uow = context.__enter__.return_value
            uow.cell_repository.load_missing_groups.return_value = Result.Success('loaded')
            module = 'src.application.image_builder.commands.load_images'
            with patch(module + '.settings', SimpleNamespace(image_groups_relative_path=root)):
                with patch(module + '.get_uow', return_value=context):
                    result = LoadImagesCommandHandler().handle(LoadImagesCommand(group))
            self.assertTrue(result.is_success)
            uow.cell_repository.load_missing_groups.assert_called_once_with(['group'])

    def test_load_rejects_other_root_before_database_access(self):
        module = 'src.application.image_builder.commands.load_images'
        with patch(module + '.settings', SimpleNamespace(image_groups_relative_path='library')):
            with patch(module + '.get_uow') as factory:
                result = LoadImagesCommandHandler().handle(LoadImagesCommand('outside'))
        self.assertFalse(result.is_success)
        factory.assert_not_called()

    def test_validate_reports_both_sides_of_mismatch_and_filters_group(self):
        with tempfile.TemporaryDirectory() as root:
            group = os.path.join(root, 'group')
            os.mkdir(group)
            on_disk, missing = os.path.join(group, 'new.png'), os.path.join(group, 'missing.png')
            context = MagicMock()
            uow = context.__enter__.return_value
            uow.file_repository.list_image_files.return_value = Result.Success([on_disk])
            uow.file_repository.resolve_image_path.side_effect = lambda path: path
            uow.cell_repository.mongo_collection.find.return_value = [{'relative_file_path': missing}]
            module = 'src.application.image_builder.commands.validate_images_data'
            with patch(module + '.settings', SimpleNamespace(image_groups_relative_path=root)):
                with patch(module + '.get_uow', return_value=context):
                    result = ValidateImagesDataCommandHandler().handle(ValidateImagesDataCommand('group'))
            self.assertFalse(result.is_success)
            self.assertIn('new.png', result.error)
            self.assertIn('missing.png', result.error)
            uow.cell_repository.mongo_collection.find.assert_called_once_with(
                {'group': 'group'}, {'relative_file_path': 1},
            )


class TestMaintenanceCli(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = importlib.import_module('src.presentation.cli.cli')

    def test_help_never_executes_commands(self):
        with patch.object(self.module.mediator, 'send', side_effect=AssertionError('unexpected command')):
            result = CliRunner().invoke(self.module.cli, ['--help'])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn('load-missing-groups', result.output)

    def test_business_error_has_nonzero_exit_and_is_logged(self):
        with patch.object(self.module.mediator, 'send', return_value=Result.Error('database unavailable')):
            with patch.object(self.module.presentation_logger, 'error') as logger:
                result = CliRunner().invoke(self.module.cli, ['load-missing-groups'])
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn('database unavailable', result.output)
        logger.assert_called_once()

    def test_unexpected_error_has_nonzero_exit_and_is_logged(self):
        with patch.object(self.module.mediator, 'send', side_effect=OSError('database unavailable')):
            with patch.object(self.module.presentation_logger, 'exception') as logger:
                result = CliRunner().invoke(self.module.cli, ['list-groups'])
        self.assertNotEqual(result.exit_code, 0)
        logger.assert_called_once()

    def test_image_data_uses_returned_result_without_second_handle(self):
        with patch.object(self.module.mediator, 'send', return_value=Result.Success(np.zeros((20, 30, 3), dtype=np.uint8))):
            result = CliRunner().invoke(self.module.cli, ['get-image-data', 'source.png'])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn('(20, 30, 3)', result.output)
        self.assertIn('uint8', result.output)
