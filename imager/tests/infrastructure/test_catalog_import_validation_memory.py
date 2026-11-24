"""Invalid pack entries never retain a decoded image into the next decode."""
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch
import weakref
import zlib

import cv2
import numpy as np

from src.infrastructure.object_storage.catalog import _local_group_plan


def float_tiff(value):
    width = height = 24
    channels, count = 4, 12
    packed = zlib.compress(np.full((height, width, channels), value, dtype=np.float64).tobytes())
    bits = 8 + 2 + 12 * count + 4
    formats, pixels = bits + 2 * channels, bits + 4 * channels
    entries = [(256,4,1,width), (257,4,1,height), (258,3,channels,bits), (259,3,1,8),
               (262,3,1,2), (273,4,1,pixels), (277,3,1,channels), (278,4,1,height),
               (279,4,1,len(packed)), (284,3,1,1), (338,3,1,2), (339,3,channels,formats)]
    return (b'II' + struct.pack('<HIH',42,8,count)
            + b''.join(struct.pack('<HHII',*entry) for entry in sorted(entries)) + bytes(4)
            + struct.pack('<4H',64,64,64,64) + struct.pack('<4H',3,3,3,3) + packed)


class TestCatalogImportValidationMemory(unittest.TestCase):
    def test_skipped_arrays_are_freed_and_invalid_entries_are_not_duplicates(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'0.tif').write_bytes(float_tiff(0))
            (root/'1.tif').write_bytes(float_tiff(1))
            cv2.imwrite(str(root/'2-small.png'), np.zeros((2,2,3), dtype=np.uint8))
            (root/'3-broken.png').write_bytes(b'not an image')
            valid = root/'4-valid.png'
            cv2.imwrite(str(valid), np.full((24,24,3),40,dtype=np.uint8))
            (root/'5-duplicate.png').write_bytes(valid.read_bytes())
            imread, references, dtypes = cv2.imread, [], []

            def monitored(path, flags):
                self.assertTrue(all(reference() is None for reference in references),
                                'Previous decoded buffer survived into the next imread')
                decoded = imread(path, flags)
                if decoded is not None:
                    references.append(weakref.ref(decoded))
                    dtypes.append(str(decoded.dtype))
                return decoded

            with patch('src.infrastructure.object_storage.catalog.cv2.imread', side_effect=monitored):
                report, _, _ = _local_group_plan('review', root, 'imager', skip_invalid=True)
            self.assertEqual(dtypes[:2], ['float64', 'float64'])
            self.assertEqual((report['source_files'], report['count'], report['invalid_files'],
                              report['duplicate_files']), (6,1,4,1))
            self.assertTrue(all(reference() is None for reference in references))

    def test_strict_directory_import_still_rejects_an_invalid_image(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'bad.tif').write_bytes(float_tiff(0))
            with self.assertRaisesRegex(ValueError, 'bad.tif:'):
                _local_group_plan('review', root, 'imager')
