import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import cv2
import numpy as np
from src.infrastructure.data.image_builder.repositories.cell_repository import CellRepository
from src.infrastructure.data.image_builder.repositories.file_repository import FileRepository


class MemoryCollection:
    def __init__(self):
        self.documents = []
        self.indexes = []
        self.next_id = 1

    def find(self, query, projection=None):
        return [dict(document) for document in self.documents
                if all(document.get(key) == value for key, value in query.items())]

    def update_one(self, query, changes, upsert=False):
        matches = [document for document in self.documents
                   if all(document.get(key) == value for key, value in query.items())]
        if matches:
            matches[0].update(changes['$set'])
        elif upsert:
            self.documents.append({'_id': self.next_id, **changes['$set']})
            self.next_id += 1

    def delete_one(self, query):
        for index, document in enumerate(self.documents):
            if all(document.get(key) == value for key, value in query.items()):
                del self.documents[index]
                break

    def create_index(self, name, unique=False):
        self.indexes.append((name, unique))


class TestCellRepository(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = self.temporary.name
        self.group = os.path.join(self.root, 'group')
        os.mkdir(self.group)
        self.files = FileRepository()
        self.files.settings = SimpleNamespace(
            image_groups_relative_path=self.root, min_size=(20, 20), max_size=(4000, 4000),
        )
        self.collection = MemoryCollection()
        CellRepository._instance = None
        self.addCleanup(setattr, CellRepository, '_instance', None)
        self.repository = CellRepository(self.collection, self.files)
        self.rss = patch('src.infrastructure.data.image_builder.repositories.cell_repository.current_rss_bytes', return_value=0)
        self.rss.start()
        self.addCleanup(self.rss.stop)

    def write_image(self, name, color):
        path = os.path.join(self.group, name)
        cv2.imwrite(path, np.full((20, 20, 3), color, dtype=np.uint8))
        return path

    def test_singleton_preserved(self):
        self.assertIs(self.repository, CellRepository(self.collection, self.files))

    def test_sync_add_replace_delete_and_deduplicate(self):
        first = self.write_image('one.png', 30)
        second = self.write_image('two.png', 90)
        self.assertTrue(self.repository.load_missing_groups().is_success)
        self.assertEqual(len(self.collection.documents), 2)
        self.collection.documents.append({**self.collection.documents[0], '_id': 999})
        self.write_image('one.png', 220)
        stat = os.stat(first)
        os.utime(first, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
        os.remove(second)
        result = self.repository.load_missing_groups()
        self.assertTrue(result.is_success, result.error)
        self.assertEqual(len(self.collection.documents), 1)
        self.assertEqual(self.collection.documents[0]['r'], 220)
        self.assertEqual(self.repository.get_all_groups_info(), [{'group': 'group', 'count': 1}])

    def test_partial_sync_refreshes_cache_and_retry_retains_good_image(self):
        path = self.write_image('good.png', 40)
        bad = os.path.join(self.group, 'bad.png')
        with open(bad, 'wb') as stream:
            stream.write(b'broken')
        self.assertFalse(self.repository.load_missing_groups().is_success)
        self.assertEqual(len(self.collection.documents), 1)
        self.assertIn(path, self.repository.data['group'])
        os.remove(bad)
        self.assertTrue(self.repository.load_missing_groups().is_success)
        self.assertIsNotNone(self.repository.find_closest_cell((40, 40, 40), 'group'))

    def test_original_cache_is_bounded_and_metadata_does_not_pin_images(self):
        self.write_image('one.png', 10)
        self.write_image('two.png', 200)
        with patch('src.infrastructure.data.image_builder.repositories.cell_repository.CACHE_LIMIT_BYTES', 1200):
            self.assertTrue(self.repository.load_missing_groups().is_success)
            for metadata in self.repository.data['group'].values():
                self.assertIsNone(metadata.image)
                self.assertTrue(self.repository.get_cell_image(metadata).is_success)
                self.assertLessEqual(self.repository.cache_bytes, 1200)
            self.assertEqual(len(self.repository._image_cache), 1)

    def test_batch_lookup_matches_individual_colors(self):
        self.write_image('black.png', 0)
        self.write_image('white.png', 255)
        self.assertTrue(self.repository.load_missing_groups().is_success)
        result = self.repository.find_closest_metadata(np.array([[1, 1, 1], [250, 250, 250]]), 'group')
        self.assertTrue(result.is_success)
        self.assertTrue(result.value[0].cell.relative_file_path.endswith('black.png'))
        self.assertTrue(result.value[1].cell.relative_file_path.endswith('white.png'))

    def test_unknown_group_is_explicit_failure(self):
        self.assertFalse(self.repository.find_closest_metadata([[0, 0, 0]], 'absent').is_success)

    def test_memory_budget_rejects_lazy_decode_before_reader(self):
        self.write_image('one.png', 10)
        self.assertTrue(self.repository.load_missing_groups().is_success)
        metadata = next(iter(self.repository.data['group'].values()))
        self.repository.trim_image_cache(0)
        with patch('src.infrastructure.data.image_builder.repositories.cell_repository.current_rss_bytes', return_value=900_000_000):
            with patch.object(self.files, 'read_image_file') as reader:
                result = self.repository.get_cell_image(metadata)
        self.assertFalse(result.is_success)
        reader.assert_not_called()

    def test_targeted_sync_does_not_remove_other_groups(self):
        other = os.path.join(self.root, 'other')
        os.mkdir(other)
        cv2.imwrite(os.path.join(other, 'image.png'), np.zeros((20, 20, 3), dtype=np.uint8))
        self.write_image('one.png', 10)
        self.assertTrue(self.repository.load_missing_groups().is_success)
        self.assertTrue(self.repository.load_missing_groups(['group']).is_success)
        self.assertEqual(len(self.collection.documents), 2)
