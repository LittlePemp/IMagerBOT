"""Direct cache uploads retain byte limits and fail before publishing changed ZIPs."""
import hashlib
import io
import os
import tempfile
import unittest
import zipfile
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, PropertyMock, patch

from minio import Minio

from imager_config.loader import config
from src.infrastructure.services.catalog_import_service import CatalogImportService
from src.infrastructure.services.catalog_import_service import service as gateway_module


def archive_bytes(payload=b'example-image-bytes'):
    result = io.BytesIO()
    with zipfile.ZipFile(result, 'w', compression=zipfile.ZIP_STORED) as archive:
        archive.writestr('image.png', payload)
    return result.getvalue()


class MissingObject(Exception):
    code = 'NoSuchKey'


class StreamingS3:
    def __init__(self):
        self.objects = {}
        self.uploads = 0
        self.bucket_checks = 0
        self.chunk_lengths = []
        self.before_read = None
        self.after_first_read = None
        self.before_stat = None
        self.fput_object = Mock(side_effect=AssertionError('Upload must not reopen the source path'))

    def bucket_exists(self, bucket):
        self.bucket_checks += 1
        return True

    def stat_object(self, bucket, key):
        if self.before_stat is not None:
            self.before_stat()
        if key not in self.objects:
            raise MissingObject()
        content, metadata = self.objects[key]
        return SimpleNamespace(size=len(content), metadata={'x-amz-meta-sha256': metadata['sha256']})

    def put_object(self, bucket, key, source, length, **kwargs):
        self.uploads += 1
        if self.before_read is not None:
            self.before_read()
        chunks = []
        remaining = length
        while remaining:
            chunk = source.read(remaining)
            if not chunk:
                raise IOError('Source ended before its declared size')
            self.chunk_lengths.append(len(chunk))
            chunks.append(chunk)
            remaining -= len(chunk)
            if len(chunks) == 1 and self.after_first_read is not None:
                self.after_first_read()
        # Like the SDK, publishing happens only after the last read succeeded.
        self.objects[key] = b''.join(chunks), kwargs['metadata']


def service_for(client=None):
    client = StreamingS3() if client is None else client
    gateway = SimpleNamespace(object_store=SimpleNamespace(client=client, bucket='imager'))
    return CatalogImportService(gateway), client


class DirectCatalogUploadTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'cached.zip'
        self.original = archive_bytes()
        self.path.write_bytes(self.original)

    async def test_reads_one_open_descriptor_and_reuses_confirmed_object(self):
        service, client = service_for()
        original_open = os.open
        with patch.object(gateway_module.os, 'open', wraps=original_open) as open_source:
            first = await service.upload_archive(self.path, 'import-direct', expected_size=len(self.original))
        second = await service.upload_archive(self.path, 'import-direct', expected_size=len(self.original))
        self.assertEqual(open_source.call_count, 1)
        self.assertEqual(first, second)
        self.assertEqual(first['sha256'], hashlib.sha256(self.original).hexdigest())
        self.assertEqual(first['size_bytes'], len(self.original))
        self.assertIn(first['sha256'], first['key'])
        self.assertEqual(client.objects[first['key']][0], self.original)
        self.assertEqual(client.uploads, 1)
        client.fput_object.assert_not_called()
        self.assertEqual(self.path.read_bytes(), self.original)

    async def test_empty_oversize_and_reported_size_mismatch_stop_before_s3(self):
        for content, limit, reported in (
            (b'', len(self.original), None),
            (self.original, len(self.original) - 1, None),
            (self.original, len(self.original), len(self.original) + 1),
            (self.original, len(self.original), True),
        ):
            with self.subTest(empty=not content, limit=limit, reported=reported):
                self.path.write_bytes(content)
                service, client = service_for()
                with patch.object(CatalogImportService, 'archive_limit', new_callable=PropertyMock, return_value=limit):
                    with self.assertRaises(ValueError):
                        await service.upload_archive(self.path, 'import-direct', expected_size=reported)
                self.assertEqual(client.bucket_checks, 0)
                self.assertEqual(client.uploads, 0)

    async def test_invalid_zip_missing_file_and_directory_stop_before_s3(self):
        self.path.write_bytes(b'not-a-zip')
        for path in (self.path, self.path.parent / 'missing.zip', self.path.parent):
            with self.subTest(kind='file' if path == self.path else path.name):
                service, client = service_for()
                with self.assertRaises(ValueError):
                    await service.upload_archive(path, 'import-direct')
                self.assertEqual(client.bucket_checks, 0)

    async def test_initial_hash_rejects_source_growth_shrink_and_in_place_change(self):
        real_sha256 = hashlib.sha256
        for mutation in ('grow', 'shrink', 'rewrite'):
            with self.subTest(mutation=mutation):
                self.path.write_bytes(self.original)
                service, client = service_for()
                path, original = self.path, self.original
                timestamp = path.stat()

                class ChangingDigest:
                    def __init__(self):
                        self.value, self.changed = real_sha256(), False

                    def update(self, chunk):
                        self.value.update(chunk)
                        if not self.changed:
                            self.changed = True
                            if mutation == 'grow':
                                path.write_bytes(original + b'X')
                            elif mutation == 'shrink':
                                path.write_bytes(original[:-1])
                            else:
                                path.write_bytes(original[:-1] + bytes((original[-1] ^ 1,)))
                                # Make this metadata-change test deterministic
                                # on Windows' deferred write-time updates. The
                                # separate restored-mtime case exercises SHA.
                                os.utime(path, ns=(timestamp.st_atime_ns, timestamp.st_mtime_ns + 1_000_000_000))

                    def hexdigest(self):
                        return self.value.hexdigest()

                with patch.object(gateway_module.hashlib, 'sha256', ChangingDigest):
                    with self.assertRaisesRegex(ValueError, 'ZIP изменился'):
                        await service.upload_archive(self.path, 'import-direct')
                self.assertEqual(client.bucket_checks, 0)
                self.assertEqual(client.objects, {})

    async def test_changed_source_cannot_publish_the_final_upload_chunk(self):
        bounded = replace(config, storage=replace(config.storage, transfer_chunk_bytes=17))
        for mutation in ('grow', 'shrink', 'rewrite'):
            with self.subTest(mutation=mutation):
                self.path.write_bytes(self.original)
                service, client = service_for()

                def change():
                    if mutation == 'grow':
                        self.path.write_bytes(self.original + b'X')
                    elif mutation == 'shrink':
                        self.path.write_bytes(self.original[:-1])
                    else:
                        self.path.write_bytes(self.original[:-1] + bytes((self.original[-1] ^ 1,)))

                client.after_first_read = change
                with patch.object(gateway_module, 'config', bounded):
                    with self.assertRaisesRegex(ValueError, 'ZIP изменился'):
                        await service.upload_archive(self.path, 'import-direct')
                self.assertEqual(client.objects, {})
                self.assertLessEqual(max(client.chunk_lengths), bounded.storage.transfer_chunk_bytes)

    async def test_existing_object_does_not_hide_a_changed_source(self):
        service, client = service_for()
        first = await service.upload_archive(self.path, 'import-direct')
        client.before_stat = lambda: self.path.write_bytes(self.original + b'X')
        with self.assertRaisesRegex(ValueError, 'ZIP изменился'):
            await service.upload_archive(self.path, 'import-direct')
        self.assertEqual(client.objects[first['key']][0], self.original)
        self.assertEqual(client.uploads, 1)

    async def test_upload_return_without_consuming_source_is_not_confirmed(self):
        service, client = service_for()
        client.put_object = Mock()
        with self.assertRaisesRegex(ValueError, 'не подтверждена'):
            await service.upload_archive(self.path, 'import-direct')

    async def test_checksum_rejects_same_size_mutation_even_if_mtime_is_restored(self):
        self.path.write_bytes(archive_bytes(b'x' * 50000))
        timestamp = self.path.stat()
        service, client = service_for()

        def change():
            with self.path.open('r+b') as stream:
                stream.seek(10000)
                stream.write(b'Y')
            os.utime(self.path, ns=(timestamp.st_atime_ns, timestamp.st_mtime_ns))

        client.before_read = change
        with self.assertRaisesRegex(ValueError, 'ZIP изменился'):
            await service.upload_archive(self.path, 'import-direct')
        self.assertEqual(client.objects, {})

    @unittest.skipIf(os.name == 'nt', 'Windows does not allow replacing this open file')
    async def test_atomic_path_replacement_keeps_uploading_the_original_open_file(self):
        # Exercise POSIX descriptor semantics on native Linux tmpfs. The main
        # test scratch can be a Windows bind mount with different rename rules.
        with tempfile.TemporaryDirectory(prefix='imager-cache-', dir='/dev/shm') as directory:
            path = Path(directory) / 'cached.zip'
            path.write_bytes(self.original)
            replacement = path.parent / 'replacement.zip'
            new_content = archive_bytes(b'replacement-image-bytes')
            replacement.write_bytes(new_content)
            service, client = service_for()
            client.before_read = lambda: os.replace(replacement, path)
            original_open = os.open
            with patch.object(gateway_module.os, 'open', wraps=original_open) as open_source:
                result = await service.upload_archive(path, 'import-direct')
            self.assertEqual(open_source.call_count, 1)
            self.assertEqual(client.objects[result['key']][0], self.original)
            self.assertEqual(result['sha256'], hashlib.sha256(self.original).hexdigest())
            self.assertEqual(path.read_bytes(), new_content)
            client.fput_object.assert_not_called()

    @unittest.skipUnless(hasattr(os, 'O_NOFOLLOW'), 'POSIX no-follow open is not available')
    async def test_final_symlink_is_not_opened(self):
        link = self.path.parent / 'link.zip'
        link.symlink_to(self.path)
        service, client = service_for()
        with self.assertRaises(ValueError):
            await service.upload_archive(link, 'import-direct')
        self.assertEqual(client.bucket_checks, 0)

    @unittest.skipUnless(hasattr(os, 'mkfifo'), 'POSIX FIFOs are not available')
    async def test_fifo_is_rejected_without_a_blocking_open(self):
        fifo = self.path.parent / 'pipe.zip'
        os.mkfifo(fifo)
        service, client = service_for()
        original_open = os.open

        def nonblocking_open(path, flags):
            self.assertTrue(flags & os.O_NONBLOCK, 'Opening a FIFO must not block')
            return original_open(path, flags)

        with patch.object(gateway_module.os, 'open', side_effect=nonblocking_open):
            with self.assertRaisesRegex(ValueError, 'обычным файлом'):
                await service.upload_archive(fifo, 'import-direct')
        self.assertEqual(client.bucket_checks, 0)

    async def test_sdk_single_part_put_is_not_called_for_changed_source(self):
        client = Minio('localhost:9000', access_key='test-user', secret_key='test-password', secure=False)
        client.bucket_exists = Mock(return_value=True)

        def change_before_read(*args):
            self.path.write_bytes(self.original + b'X')
            raise MissingObject()

        client.stat_object = Mock(side_effect=change_before_read)
        client._put_object = Mock()
        client._create_multipart_upload = Mock()
        service, _ = service_for(client)
        with self.assertRaisesRegex(ValueError, 'ZIP изменился'):
            await service.upload_archive(self.path, 'import-direct')
        client._put_object.assert_not_called()
        client._create_multipart_upload.assert_not_called()

    async def test_sdk_aborts_multipart_before_completion_on_changed_source(self):
        self.path.write_bytes(archive_bytes(b'x' * (6 * 1024 * 1024)))
        client = Minio('localhost:9000', access_key='test-user', secret_key='test-password', secure=False)
        client.bucket_exists = Mock(return_value=True)
        client.stat_object = Mock(side_effect=MissingObject())
        client._create_multipart_upload = Mock(return_value='test-upload')

        def mutate_after_first_part(*args):
            with self.path.open('r+b') as stream:
                stream.seek(-1, os.SEEK_END)
                stream.write(b'X')
            return 'test-etag'

        client._upload_part = Mock(side_effect=mutate_after_first_part)
        client._complete_multipart_upload = Mock()
        client._abort_multipart_upload = Mock()
        client._put_object = Mock()
        service, _ = service_for(client)
        with self.assertRaisesRegex(ValueError, 'ZIP изменился'):
            await service.upload_archive(self.path, 'import-direct')
        client._upload_part.assert_called_once()
        client._abort_multipart_upload.assert_called_once()
        client._complete_multipart_upload.assert_not_called()
        client._put_object.assert_not_called()
