import io
import stat
import struct
import tempfile
import unittest
import zipfile
import zlib
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from imager_config.loader import config
from src.application.catalog_import.archive import extract_catalog_archive
from src.application.catalog_import.errors import InvalidCatalogArchive


class TestCatalogImportArchive(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.addCleanup(self.temporary.cleanup)

    def make_zip(self, members, *, method=zipfile.ZIP_STORED):
        path = self.root / f'{uuid4().hex}.zip'
        with zipfile.ZipFile(path, 'w', compression=method) as archive:
            for name, value in members:
                archive.writestr(name, value)
        # ZipInfo writing normalizes Windows separators on Windows. Forge the
        # actual archive bytes so the hostile member survives to the reader.
        for name, _ in members:
            if isinstance(name, str) and '\\' in name:
                path.write_bytes(path.read_bytes().replace(name.replace('\\', '/').encode(), name.encode()))
        return path

    def extract(self, path, **changes):
        policy = replace(config.pack_import, **changes)
        destination = self.root / uuid4().hex
        report = extract_catalog_archive(path, destination, policy=policy)
        return report, destination

    def test_nested_safe_images_are_flattened_and_os_metadata_is_not_extracted(self):
        path = self.make_zip([('pack/', b''), ('pack/a.PNG', b'png'),
                              ('pack/nested/a.png', b'other'), ('__MACOSX/._a.png', b'metadata'),
                              ('pack/.DS_Store', b'metadata')])
        report, directory = self.extract(path)
        self.assertEqual(report, {'files': 2, 'expanded_bytes': 8, 'skipped': 0, 'skipped_samples': [],
                                  'origins': ['pack/a.PNG', 'pack/nested/a.png']})
        self.assertEqual(sorted(item.name for item in directory.iterdir()), ['00000000.png', '00000001.png'])
        self.assertEqual((directory / '00000001.png').read_bytes(), b'other')

    def test_stray_files_are_skipped_and_reported_instead_of_rejecting_the_pack(self):
        path = self.make_zip([('a.png', b'png'), ('readme.txt', b'notes'), ('clip.mp4', b'video'),
                              ('empty.png', b'')])
        report, directory = self.extract(path)
        self.assertEqual(report['files'], 1)
        self.assertEqual(report['skipped'], 3)
        self.assertEqual(len(report['skipped_samples']), 3)
        self.assertEqual([item.name for item in directory.iterdir()], ['00000000.png'])

    def test_extensionless_members_are_identified_by_signature(self):
        jpeg, png = b'\xff\xd8\xff\xe0' + b'J' * 20, b'\x89PNG\r\n\x1a\n' + b'P' * 20
        webp = b'RIFF\x10\x00\x00\x00WEBPVP8 ' + b'W' * 8
        path = self.make_zip([('htt', jpeg), ('icon', png), ('sticker', webp), ('notes', b'plain text')])
        report, directory = self.extract(path)
        self.assertEqual(report['files'], 3)
        self.assertEqual(report['skipped'], 1)
        names = sorted(item.name for item in directory.iterdir())
        self.assertEqual(names, ['00000000.jpg', '00000001.png', '00000002.webp'])
        self.assertEqual((directory / '00000000.jpg').read_bytes(), jpeg)

    def test_archive_without_any_image_is_still_rejected(self):
        with self.assertRaises(InvalidCatalogArchive):
            self.extract(self.make_zip([('readme', b'just text'), ('notes.txt', b'more')]))

    def test_unsafe_paths_are_rejected_before_destination_files(self):
        for name in ('../evil.png', '/evil.png', 'C:/evil.png', 'dir\\evil.png',
                     'dir/./evil.png', 'dir//evil.png', 'dir/../evil.png', 'file.png:stream', 'bad\x01.png'):
            with self.subTest(name=name):
                with self.assertRaises(InvalidCatalogArchive):
                    self.extract(self.make_zip([(name, b'data')]))
        self.assertFalse((self.root / 'evil.png').exists())

    def test_symlinks_and_special_members_are_rejected(self):
        for mode in (stat.S_IFLNK, stat.S_IFIFO, stat.S_IFCHR):
            info = zipfile.ZipInfo('file.png')
            info.create_system = 3
            info.external_attr = (mode | 0o777) << 16
            with self.subTest(mode=mode):
                with self.assertRaises(InvalidCatalogArchive):
                    self.extract(self.make_zip([(info, b'../target')]))

    def test_duplicate_casefold_and_unicode_names_are_rejected(self):
        for names in (('A.png', 'a.png'), ('caf\u00e9.png', 'cafe\u0301.png')):
            with self.subTest(names=names):
                with self.assertRaises(InvalidCatalogArchive):
                    self.extract(self.make_zip([(name, b'data') for name in names]))

    def test_file_directory_conflict_is_rejected_in_either_order(self):
        for names in (('a.png', 'a.png/b.png'), ('a.png/b.png', 'a.png')):
            with self.subTest(names=names):
                with self.assertRaises(InvalidCatalogArchive):
                    self.extract(self.make_zip([(name, b'data') for name in names]))

    def test_entry_count_is_verified_before_zipfile_constructor_even_if_eocd_lies(self):
        path = self.make_zip([(f'{index}.png', b'x') for index in range(3)])
        data = bytearray(path.read_bytes())
        offset = data.rfind(b'PK\x05\x06')
        struct.pack_into('<HH', data, offset + 8, 1, 1)
        path.write_bytes(data)
        with patch('src.application.catalog_import.archive.zipfile.ZipFile', side_effect=AssertionError('allocated ZipInfo')):
            with self.assertRaises(InvalidCatalogArchive):
                self.extract(path, max_entries=2)

    def test_central_directory_memory_is_bounded_before_constructor(self):
        path = self.make_zip([('a.png', b'x')])
        with patch('src.application.catalog_import.archive.zipfile.ZipFile', side_effect=AssertionError('allocated ZipInfo')):
            with self.assertRaises(InvalidCatalogArchive):
                self.extract(path, max_central_directory_bytes=10)

    def test_eocd_gap_is_rejected_before_constructor(self):
        path = self.make_zip([('a.png', b'x')])
        data = path.read_bytes()
        offset = data.rfind(b'PK\x05\x06')
        path.write_bytes(data[:offset] + b'gap!' + data[offset:])
        with self.assertRaises(InvalidCatalogArchive):
            self.extract(path)

    def test_malformed_utf8_filename_becomes_permanent_archive_error(self):
        path = self.make_zip([('a.png', b'x')])
        data = bytearray(path.read_bytes())
        central = data.index(b'PK\x01\x02')
        struct.pack_into('<H', data, central + 8, 0x800)
        data[central + 46] = 0xff
        path.write_bytes(data)
        with self.assertRaises(InvalidCatalogArchive):
            self.extract(path)

    def test_encrypted_and_unsupported_methods_are_rejected(self):
        path = self.make_zip([('a.png', b'x')])
        data = bytearray(path.read_bytes())
        central = data.index(b'PK\x01\x02')
        struct.pack_into('<H', data, central + 8, 1)
        path.write_bytes(data)
        with self.assertRaises(InvalidCatalogArchive):
            self.extract(path)
        with self.assertRaises(InvalidCatalogArchive):
            self.extract(self.make_zip([('a.png', b'x')], method=zipfile.ZIP_BZIP2))

    def test_archive_file_expanded_ratio_and_path_budgets(self):
        path = self.make_zip([('a.png', b'12345'), ('b.png', b'12345')])
        for policy in ({'max_archive_bytes': 1}, {'max_file_bytes': 4},
                       {'max_expanded_bytes': 9}, {'max_path_bytes': 4}):
            with self.subTest(policy=policy):
                with self.assertRaises(InvalidCatalogArchive):
                    self.extract(path, **policy)
        with self.assertRaises(InvalidCatalogArchive):
            self.extract(self.make_zip([('a/b/c.png', b'x')]), max_path_depth=2)
        bomb = self.make_zip([('a.png', b'x' * 20000)], method=zipfile.ZIP_DEFLATED)
        with self.assertRaises(InvalidCatalogArchive):
            self.extract(bomb, max_compression_ratio=2)

    def test_crc_tampering_and_truncation_are_rejected(self):
        path = self.make_zip([('a.png', b'original')])
        data = bytearray(path.read_bytes())
        data[data.index(b'original')] = ord('X')
        path.write_bytes(data)
        with self.assertRaises(InvalidCatalogArchive):
            self.extract(path)
        path.write_bytes(b'PK\x03\x04truncated')
        with self.assertRaises(InvalidCatalogArchive):
            self.extract(path)

    def test_nested_archives_empty_images_and_nonimages_are_rejected(self):
        for member in (('nested.zip', b'PK'), ('readme.txt', b'notes'), ('empty.png', b''), ('folder/', b'data')):
            with self.subTest(member=member):
                with self.assertRaises(InvalidCatalogArchive):
                    self.extract(self.make_zip([member]))
        with self.assertRaises(InvalidCatalogArchive):
            self.extract(self.make_zip([('.DS_Store', b'metadata')]))

    def test_forged_small_size_cannot_hide_extra_inflated_bytes_after_image_prefix(self):
        prefix = b'pretend valid image prefix'
        path = self.make_zip([('a.png', prefix + b'X' * 200000)], method=zipfile.ZIP_DEFLATED)
        data = bytearray(path.read_bytes())
        central = data.index(b'PK\x01\x02')
        # ZipExtFile would truncate to this size and verify only the prefix CRC.
        struct.pack_into('<L', data, central + 16, zlib.crc32(prefix))
        struct.pack_into('<L', data, central + 24, len(prefix))
        path.write_bytes(data)
        with zipfile.ZipFile(path) as vulnerable_reader:
            self.assertEqual(vulnerable_reader.read('a.png'), prefix)
        with self.assertRaises(InvalidCatalogArchive):
            self.extract(path)

    def test_stored_member_forged_size_is_rejected(self):
        prefix = b'image'
        path = self.make_zip([('a.png', prefix + b'extra')])
        data = bytearray(path.read_bytes())
        central = data.index(b'PK\x01\x02')
        struct.pack_into('<L', data, central + 16, zlib.crc32(prefix))
        struct.pack_into('<L', data, central + 24, len(prefix))
        path.write_bytes(data)
        with self.assertRaises(InvalidCatalogArchive):
            self.extract(path)

    def test_deflate_stream_uses_multiple_bounded_output_chunks(self):
        payload = b'pattern' * 20000
        path = self.make_zip([('a.png', payload)], method=zipfile.ZIP_DEFLATED)
        report, output = self.extract(path, extract_chunk_bytes=1024, max_compression_ratio=2000)
        self.assertEqual(report['expanded_bytes'], len(payload))
        self.assertEqual((output / '00000000.png').read_bytes(), payload)

    def test_valid_deflate_end_bits_and_pending_output_are_drained(self):
        for chunk in (1, 2, 3, 7, 16, 64, 128, 1024):
            for payload in (b'x' * 31, b'x' * 1024, bytes(range(256)) * 4):
                with self.subTest(chunk=chunk, size=len(payload)):
                    path = self.make_zip([('a.png', payload)], method=zipfile.ZIP_DEFLATED)
                    report, output = self.extract(path, extract_chunk_bytes=chunk, max_compression_ratio=2000)
                    self.assertEqual(report['expanded_bytes'], len(payload))
                    self.assertEqual((output / '00000000.png').read_bytes(), payload)

    def test_trailing_data_inside_declared_compressed_range_is_rejected(self):
        path = self.make_zip([('a.png', b'image bytes')], method=zipfile.ZIP_DEFLATED)
        data = bytearray(path.read_bytes())
        central = data.index(b'PK\x01\x02')
        compressed_size = struct.unpack_from('<L', data, central + 20)[0]
        data[central:central] = b'junk'
        struct.pack_into('<L', data, central + 4 + 20, compressed_size + 4)
        eocd = data.rfind(b'PK\x05\x06')
        struct.pack_into('<L', data, eocd + 16, central + 4)
        path.write_bytes(data)
        with self.assertRaises(InvalidCatalogArchive):
            self.extract(path)
