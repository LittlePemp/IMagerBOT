import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import cv2
import numpy as np
from src.infrastructure.data.image_builder.repositories.file_repository import FileRepository


class TestFileRepository(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = self.temporary.name
        self.repository = FileRepository()
        self.repository.settings = SimpleNamespace(
            file_path_prefix='', generated_images_path=self.root,
            image_groups_relative_path=self.root, min_size=(20, 20), max_size=(4000, 4000),
        )
        self.image = np.full((20, 30, 3), 125, dtype=np.uint8)

    def test_real_image_roundtrip_and_atomic_publish(self):
        destination = os.path.join(self.root, 'result.png')
        self.assertTrue(self.repository.save_image_file(self.image, destination).is_success)
        result = self.repository.read_image_file(destination)
        self.assertTrue(result.is_success, result.error)
        np.testing.assert_array_equal(result.value, self.image)
        self.assertEqual(os.listdir(self.root), ['result.png'])

    def test_encoder_false_is_failure_and_does_not_replace_previous_result(self):
        destination = os.path.join(self.root, 'result.png')
        with open(destination, 'wb') as stream:
            stream.write(b'previous-result')
        with patch('cv2.imwrite', return_value=False):
            result = self.repository.save_image_file(self.image, destination)
        self.assertFalse(result.is_success)
        with open(destination, 'rb') as stream:
            self.assertEqual(stream.read(), b'previous-result')
        self.assertEqual(os.listdir(self.root), ['result.png'])

    def test_encoder_exception_removes_temporary_file(self):
        destination = os.path.join(self.root, 'result.png')
        with patch('cv2.imwrite', side_effect=OSError('disk full')):
            result = self.repository.save_image_file(self.image, destination)
        self.assertFalse(result.is_success)
        self.assertEqual(os.listdir(self.root), [])

    def test_save_rejects_path_outside_generated_root(self):
        destination = os.path.join(self.root, '..', 'outside.png')
        with patch('cv2.imwrite') as writer:
            result = self.repository.save_image_file(self.image, destination)
        self.assertFalse(result.is_success)
        writer.assert_not_called()

    def test_missing_file_is_rejected_before_decoder(self):
        with patch('cv2.imread') as reader:
            result = self.repository.read_image_file(os.path.join(self.root, 'absent.png'))
        self.assertFalse(result.is_success)
        reader.assert_not_called()

    def test_corrupt_file_is_rejected(self):
        path = os.path.join(self.root, 'bad.png')
        with open(path, 'wb') as stream:
            stream.write(b'not an image')
        self.assertFalse(self.repository.read_image_file(path).is_success)

    def test_encoded_size_limit_is_checked_before_decoder(self):
        path = os.path.join(self.root, 'image.png')
        cv2.imwrite(path, self.image)
        with patch('src.infrastructure.data.image_builder.repositories.file_repository.MAX_SOURCE_FILE_BYTES', 1):
            with patch('cv2.imread') as reader:
                result = self.repository.read_image_file(path)
        self.assertFalse(result.is_success)
        reader.assert_not_called()

    def test_list_skips_image_named_directory(self):
        cv2.imwrite(os.path.join(self.root, 'image.png'), self.image)
        os.mkdir(os.path.join(self.root, 'folder.jpg'))
        result = self.repository.list_image_files(self.root)
        self.assertTrue(result.is_success)
        self.assertEqual(result.value, [os.path.join(self.root, 'image.png')])
