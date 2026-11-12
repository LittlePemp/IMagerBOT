"""Publish catalog-import commands without a Temporal dependency in the bot."""
import asyncio
import hashlib
import os
import re
import stat
import zipfile
from contextlib import contextmanager
from copy import deepcopy

from pymongo.errors import DuplicateKeyError, PyMongoError
from pymongo.write_concern import WriteConcern

from imager_config.loader import config
from imager_contracts import ObjectRef
from imager_contracts.catalog_import import (
    CatalogImportRequest, ZIP_CONTENT_TYPE, import_fingerprint, make_import_submission,
)
from imager_contracts.protocol import JOB_ID_PATTERN
from settings import settings


ZIP_SIGNATURE = b'PK\x03\x04'
TERMINAL_STATES = ('completed', 'failed')


def _file_state(stream):
    value = os.fstat(stream.fileno())
    # Renaming/replacing the cache path can change the old inode's ctime while
    # our already-open descriptor still holds the same, valid ZIP bytes.
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns)


@contextmanager
def _open_archive(path):
    flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_NOFOLLOW', 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ValueError('ZIP недоступен для чтения. Повторите загрузку архива.') from error
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError('ZIP должен быть обычным файлом.')
        # Read the current descriptor bytes rather than a prefetched Python
        # buffer: the upload checksum must also detect in-place cache changes.
        with os.fdopen(descriptor, 'rb', buffering=0, closefd=False) as stream:
            yield stream
    finally:
        os.close(descriptor)


def _archive_checksum(stream, state):
    checksum = hashlib.sha256()
    remaining = state[2]
    while remaining:
        chunk = stream.read(min(remaining, config.storage.transfer_chunk_bytes))
        if not chunk:
            raise ValueError('ZIP изменился во время проверки.')
        remaining -= len(chunk)
        checksum.update(chunk)
    if stream.read(1) or _file_state(stream) != state:
        raise ValueError('ZIP изменился во время проверки.')
    return checksum.hexdigest()


class _VerifiedArchiveReader:
    """Reject changed input before MinIO receives its final upload chunk."""

    def __init__(self, stream, state, checksum):
        self.stream, self.state, self.checksum = stream, state, checksum
        self.digest, self.consumed, self.verified = hashlib.sha256(), 0, False

    def read(self, size=-1):
        if self.verified or size == 0:
            return b''
        count = config.storage.transfer_chunk_bytes if size < 0 else min(size, config.storage.transfer_chunk_bytes)
        chunk = self.stream.read(min(count, self.state[2] - self.consumed))
        if not chunk:
            raise ValueError('ZIP изменился во время загрузки.')
        self.consumed += len(chunk)
        self.digest.update(chunk)
        if self.consumed == self.state[2]:
            # put_object reads its last part before _put_object / multipart
            # completion. Raising here aborts upload without publishing an
            # object under a checksum that does not match its actual bytes.
            if (self.stream.read(1) or _file_state(self.stream) != self.state
                    or self.digest.hexdigest() != self.checksum):
                raise ValueError('ZIP изменился во время загрузки.')
            self.verified = True
        return chunk


class ImportSubmissionUncertain(RuntimeError):
    def __init__(self, import_id):
        self.import_id = import_id
        super().__init__('Подтверждение приёма импорта пока недоступно.')


class CatalogImportService:
    def __init__(self, core_gateway, *, upload_gate=None):
        self.core_gateway = core_gateway
        self.upload_gate = upload_gate
        self._submission_lock = asyncio.Lock()

    @property
    def archive_limit(self):
        return (config.pack_import.max_archive_bytes if getattr(settings, 'telegram_api_local', False)
                else min(config.pack_import.max_archive_bytes, config.pack_import.cloud_max_archive_bytes))

    @property
    def jobs(self):
        return self.core_gateway.database[config.pack_import.jobs_collection].with_options(
            write_concern=WriteConcern(w=1, j=True),
        )

    async def ensure_indexes(self):
        await asyncio.to_thread(
            self.jobs.create_index, [('request.actor_id', 1), ('created_at', -1)],
            name='bot_import_owner_history',
        )

    @staticmethod
    def prepare(request):
        return make_import_submission(request)

    async def get_owned(self, import_id, actor_id):
        if not isinstance(import_id, str) or not re.fullmatch(JOB_ID_PATTERN, import_id):
            raise ValueError('Эта загрузка недоступна. Откройте список паков заново.')
        return await asyncio.to_thread(
            self.jobs.find_one, {'_id': import_id, 'request.actor_id': actor_id},
        )

    async def recent(self, actor_id):
        return await asyncio.to_thread(lambda: list(
            self.jobs.find({'request.actor_id': actor_id})
            .sort('created_at', -1).limit(config.pack_import.recent_limit)
        ))

    async def unsynced_completed(self, limit):
        rows = await asyncio.to_thread(lambda: list(
            self.jobs.find({'status': 'completed', 'bot_catalog_synced': {'$ne': True}}, {'_id': 1})
            .sort('updated_at', 1).limit(limit)
        ))
        return [row['_id'] for row in rows]

    async def mark_catalog_synced(self, import_ids):
        if import_ids:
            await asyncio.to_thread(
                self.jobs.update_many,
                {'_id': {'$in': list(import_ids)}, 'status': 'completed'},
                {'$set': {'bot_catalog_synced': True}},
            )

    def _check_capacity(self, actor_id):
        query = {'status': {'$nin': list(TERMINAL_STATES)}}
        if self.jobs.count_documents(dict(query, **{'request.actor_id': actor_id})) >= config.pack_import.max_pending_per_admin:
            raise ValueError('Ваш предыдущий импорт ещё выполняется. Сначала дождитесь его завершения.')
        if self.jobs.count_documents(query) >= config.pack_import.max_pending_global:
            raise ValueError('Очередь импорта заполнена. Попробуйте позже.')

    async def ensure_capacity(self, actor_id, import_id):
        if await self.get_owned(import_id, actor_id) is None:
            await asyncio.to_thread(self._check_capacity, actor_id)

    async def submit(self, document):
        request = CatalogImportRequest.from_dict(document['request'])
        if document.get('_id') != request.import_id or document.get('payload_hash') != import_fingerprint(request):
            raise ValueError('Некорректный снимок запроса импорта.')
        if document.get('status') != 'queued' or document.get('dispatch', {}).get('state') != 'pending':
            raise ValueError('Ожидается новый подготовленный запрос импорта.')
        async with self._submission_lock:
            return await asyncio.to_thread(self._submit, deepcopy(document), request)

    def _submit(self, document, request):
        existing = self.jobs.find_one({'_id': request.import_id})
        if existing is not None:
            return self._matching(existing, document)
        self._check_capacity(request.actor_id)
        try:
            try:
                self.jobs.update_one(
                    {'_id': request.import_id}, {'$setOnInsert': document}, upsert=True,
                )
            except DuplicateKeyError:
                pass
            stored = self.jobs.find_one({'_id': request.import_id})
            if stored is None:
                raise ImportSubmissionUncertain(request.import_id)
            return self._matching(stored, document)
        except PyMongoError as error:
            try:
                stored = self.jobs.find_one({'_id': request.import_id})
                if stored is not None:
                    return self._matching(stored, document)
            except PyMongoError:
                pass
            raise ImportSubmissionUncertain(request.import_id) from error

    @staticmethod
    def _matching(stored, document):
        if stored.get('payload_hash') != document['payload_hash']:
            raise ValueError('Этот архив уже принят с другим названием или содержимым.')
        return stored

    async def upload_archive(self, path, import_id, *, expected_size=None):
        return await asyncio.to_thread(self._upload_archive, path, import_id, expected_size=expected_size)

    def _upload_archive(self, path, import_id, *, expected_size=None):
        if not isinstance(import_id, str) or not re.fullmatch(JOB_ID_PATTERN, import_id):
            raise ValueError('Не удалось принять архив. Начните загрузку заново.')
        with _open_archive(path) as stream:
            state = _file_state(stream)
            size = state[2]
            if not 0 < size <= self.archive_limit:
                raise ValueError('ZIP превышает допустимый размер или пуст.')
            if expected_size is not None and (type(expected_size) is not int or expected_size != size):
                raise ValueError('Размер ZIP не совпадает с размером, указанным Telegram.')
            if stream.read(len(ZIP_SIGNATURE)) != ZIP_SIGNATURE:
                raise ValueError('Файл не является поддерживаемым ZIP-архивом.')
            if not zipfile.is_zipfile(stream):
                raise ValueError('ZIP-архив повреждён.')
            stream.seek(0)
            checksum = _archive_checksum(stream, state)
            store = self.core_gateway.object_store
            key = config.pack_import.archive_key_template.format(import_id=import_id, sha256=checksum)
            if not store.client.bucket_exists(store.bucket):
                try:
                    store.client.make_bucket(store.bucket)
                except Exception as error:
                    if getattr(error, 'code', None) not in ('BucketAlreadyExists', 'BucketAlreadyOwnedByYou'):
                        raise
            try:
                existing = store.client.stat_object(store.bucket, key)
            except Exception as error:
                if getattr(error, 'code', None) not in ('NoSuchKey', 'NoSuchObject', 'NotFound'):
                    raise
            else:
                metadata = {str(name).lower(): str(value) for name, value in existing.metadata.items()}
                if existing.size != size or metadata.get('x-amz-meta-sha256') != checksum:
                    raise ValueError('Сохранённый архив не соответствует его контрольной сумме.')
                stream.seek(0)
                if _archive_checksum(stream, state) != checksum:
                    raise ValueError('ZIP изменился во время проверки.')
                return ObjectRef(store.bucket, key, checksum, size, ZIP_CONTENT_TYPE).to_dict()
            stream.seek(0)
            reader = _VerifiedArchiveReader(stream, state, checksum)
            store.client.put_object(
                store.bucket, key, reader, size, content_type=ZIP_CONTENT_TYPE,
                metadata={'sha256': checksum}, num_parallel_uploads=config.storage.upload_parallelism,
            )
            if not reader.verified:
                raise ValueError('Загрузка ZIP не подтверждена полностью.')
            return ObjectRef(store.bucket, key, checksum, size, ZIP_CONTENT_TYPE).to_dict()
