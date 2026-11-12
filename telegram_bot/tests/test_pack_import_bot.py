"""Pack commands and Telegram conversations run without external services."""
import asyncio
import copy
import hashlib
import io
import tempfile
import unittest
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import SimpleEventIsolation
from aiogram.methods import GetFile
from aiogram.types import Chat, Document, File, Message, Update, User as TelegramUser
from pymongo.errors import NetworkTimeout

from imager_config.loader import config
from imager_contracts import ObjectRef
from imager_contracts.catalog_import import CatalogImportRequest, ZIP_CONTENT_TYPE
from src.commands.admin_panel.pack_import import handlers
from src.commands.main_menu import register_handlers_main_menu, register_fallback
from src.infrastructure.services.catalog_import_service import CatalogImportService, ImportSubmissionUncertain
from src.infrastructure.services.catalog_import_service import service as gateway_module
from src.models.user import User, UserStatus
from src.utils.middlewares.authentication_middleware import AuthenticationMiddleware
from src.utils.navigation import NavigationMiddleware
from test_bot_routes import FakeTelegramSession


def zip_bytes():
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w') as archive:
        archive.writestr('image.png', b'not-decoded-in-bot')
    return output.getvalue()


def value_at(document, key):
    value = document
    for part in key.split('.'):
        value = value.get(part) if isinstance(value, dict) else None
    return value


def matching(document, query):
    for key, condition in query.items():
        value = value_at(document, key)
        if isinstance(condition, dict) and '$nin' in condition:
            if value in condition['$nin']:
                return False
        elif value != condition:
            return False
    return True


class Cursor:
    def __init__(self, rows):
        self.rows = rows

    def sort(self, key, direction):
        self.rows.sort(key=lambda row: value_at(row, key), reverse=direction < 0)
        return self

    def limit(self, limit):
        return self.rows[:limit]


class Jobs:
    def __init__(self):
        self.documents = {}
        self.fail_after_write = False
        self.reads = 0

    def with_options(self, **options):
        self.write_concern = options['write_concern']
        return self

    def create_index(self, *args, **kwargs):
        pass

    def find_one(self, query):
        self.reads += 1
        return copy.deepcopy(next((doc for doc in self.documents.values() if matching(doc, query)), None))

    def count_documents(self, query):
        return sum(matching(doc, query) for doc in self.documents.values())

    def update_one(self, query, update, **kwargs):
        self.documents.setdefault(query['_id'], copy.deepcopy(update['$setOnInsert']))
        if self.fail_after_write:
            self.fail_after_write = False
            raise NetworkTimeout('Acknowledgement lost after insert')

    def find(self, query):
        return Cursor([copy.deepcopy(doc) for doc in self.documents.values() if matching(doc, query)])


class MissingObject(Exception):
    code = 'NoSuchKey'


class S3:
    def __init__(self):
        self.objects = {}
        self.uploads = 0

    def bucket_exists(self, bucket):
        return True

    def stat_object(self, bucket, key):
        if key not in self.objects:
            raise MissingObject()
        content, metadata = self.objects[key]
        return SimpleNamespace(size=len(content), metadata={'x-amz-meta-sha256': metadata['sha256']})

    def fput_object(self, bucket, key, path, **kwargs):
        self.uploads += 1
        self.objects[key] = Path(path).read_bytes(), kwargs['metadata']


def request(import_id='import-1', actor=999, name='Иконки'):
    content = zip_bytes()
    return CatalogImportRequest(
        import_id, actor, f'pack_{import_id.replace("-", "_")}', name,
        ObjectRef('imager', f'catalog/imports/{import_id}/source.zip', hashlib.sha256(content).hexdigest(), len(content), ZIP_CONTENT_TYPE),
    )


def gateway():
    jobs, store = Jobs(), S3()
    core = SimpleNamespace(
        database={config.pack_import.jobs_collection: jobs}, object_store=SimpleNamespace(client=store, bucket='imager'),
        list_groups=AsyncMock(return_value=SimpleNamespace(groups=[])),
    )
    return CatalogImportService(core), jobs, store


class PackImportGatewayTests(unittest.IsolatedAsyncioTestCase):
    async def test_snapshot_is_durable_idempotent_and_uses_journaled_write(self):
        service, jobs, _ = gateway()
        document = service.prepare(request())
        first = await service.submit(document)
        changed_policy = copy.deepcopy(document)
        changed_policy['execution_policy']['import']['maximum_attempts'] += 1
        again = await service.submit(changed_policy)
        self.assertEqual(first, again)
        self.assertEqual(len(jobs.documents), 1)
        self.assertTrue(jobs.write_concern.document['j'])

    async def test_same_identity_with_changed_name_is_rejected(self):
        service, jobs, _ = gateway()
        await service.submit(service.prepare(request()))
        with self.assertRaises(ValueError):
            await service.submit(service.prepare(request(name='Другой пак')))
        self.assertEqual(len(jobs.documents), 1)

    async def test_lost_ack_does_not_create_another_import(self):
        service, jobs, _ = gateway()
        jobs.fail_after_write = True
        accepted = await service.submit(service.prepare(request()))
        self.assertEqual(accepted['_id'], 'import-1')
        self.assertEqual(len(jobs.documents), 1)

    async def test_owned_lookup_and_invalid_identifier_before_database(self):
        service, jobs, _ = gateway()
        await service.submit(service.prepare(request()))
        self.assertIsNone(await service.get_owned('import-1', 2))
        reads = jobs.reads
        with self.assertRaises(ValueError):
            await service.get_owned('../unsafe', 999)
        self.assertEqual(jobs.reads, reads)

    async def test_pending_limit_does_not_block_duplicate(self):
        service, jobs, _ = gateway()
        for index in range(config.pack_import.max_pending_per_admin):
            await service.submit(service.prepare(request(import_id=f'import-{index}')))
        duplicate = await service.submit(service.prepare(request(import_id='import-0')))
        self.assertEqual(duplicate['_id'], 'import-0')
        with self.assertRaises(ValueError):
            await service.submit(service.prepare(request(import_id='extra')))

    async def test_archive_validation_and_content_addressed_reuse(self):
        service, _, store = gateway()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'test.zip'
            path.write_bytes(b'not zip')
            with self.assertRaises(ValueError):
                await service.upload_archive(path, 'import-1')
            path.write_bytes(zip_bytes())
            first = await service.upload_archive(path, 'import-1')
            again = await service.upload_archive(path, 'import-1')
        self.assertEqual(first, again)
        self.assertEqual(store.uploads, 1)
        self.assertEqual(first['content_type'], ZIP_CONTENT_TYPE)

    async def test_local_api_allows_configured_limit_and_cloud_fallback_is_bounded(self):
        service, _, _ = gateway()
        with patch.object(gateway_module, 'settings', SimpleNamespace(telegram_api_local=True)):
            self.assertEqual(service.archive_limit, config.pack_import.max_archive_bytes)
        with patch.object(gateway_module, 'settings', SimpleNamespace(telegram_api_local=False)):
            self.assertEqual(service.archive_limit, min(config.pack_import.max_archive_bytes, config.pack_import.cloud_max_archive_bytes))


class ZipTelegramSession(FakeTelegramSession):
    def __init__(self):
        super().__init__()
        self.archive = zip_bytes()
        self.downloads = 0

    async def make_request(self, bot, method, timeout=None):
        if isinstance(method, GetFile):
            self.methods.append(method)
            return File(file_id='zip-file', file_unique_id='zip-unique', file_size=len(self.archive), file_path='source.zip')
        return await super().make_request(bot, method, timeout)

    async def stream_content(self, url, **kwargs):
        self.downloads += 1
        yield self.archive


class PackImportConversationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.session = ZipTelegramSession()
        self.bot = Bot(token='123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi', session=self.session)
        self.service, self.jobs, self.store = gateway()
        now = datetime.now(timezone.utc)
        self.people = {actor: User(
            telegram_id=actor, telegram_username=None, name='Тест',
            registered_datetime_utc=now, last_activity_datetime_utc=now, isbanned=False, status=status,
        ) for actor, status in ((999, UserStatus.USER), (2, UserStatus.ADMIN), (3, UserStatus.USER))}
        self.users = SimpleNamespace(
            get_user_by_id=AsyncMock(side_effect=lambda actor: self.people.get(actor)), update_user_info=AsyncMock(),
        )
        self.groups = SimpleNamespace(sync_groups=AsyncMock())
        self.dp = Dispatcher(events_isolation=SimpleEventIsolation())
        self.dp['uow'] = SimpleNamespace(user_repository=self.users, image_group_repository=self.groups)
        self.dp['user_repository'] = self.users
        self.dp['catalog_import_service'] = self.service
        for observer in (self.dp.message, self.dp.callback_query):
            observer.outer_middleware(AuthenticationMiddleware(self.users))
        self.dp.callback_query.outer_middleware(NavigationMiddleware())
        register_handlers_main_menu(self.dp)
        handlers.register_handlers_pack_import(self.dp)
        register_fallback(self.dp)
        self.sequence = 0

    async def asyncTearDown(self):
        await self.dp.fsm.close()
        await self.bot.session.close()
        self.assertEqual(self.session.unsupported, [])

    def state(self, actor=999):
        return self.dp.fsm.get_context(bot=self.bot, chat_id=actor, user_id=actor)

    async def send(self, text=None, *, actor=999, filename=None, size=None):
        self.sequence += 1
        document = Document(file_id='zip-file', file_unique_id='zip-unique', file_name=filename,
                            file_size=len(self.session.archive) if size is None else size) if filename else None
        message = Message(
            message_id=self.sequence, date=datetime.now(timezone.utc), chat=Chat(id=actor, type='private'),
            from_user=TelegramUser(id=actor, is_bot=False, first_name='Тест'), text=text, document=document,
        )
        await self.dp.feed_update(self.bot, Update(update_id=self.sequence, message=message))
        return self.session.last.get(actor)

    async def test_zip_then_name_imports_once_and_status_synchronizes_completed_pack(self):
        await self.send('/import_pack')
        await self.send(filename='icons.zip')
        self.assertEqual(self.session.downloads, 0)
        self.assertEqual(await self.state().get_state(), handlers.PackImportStates.waiting_for_name.state)
        await self.send('Новые иконки')
        self.assertEqual(self.session.downloads, 1)
        self.assertEqual(len(self.jobs.documents), 1)
        job = next(iter(self.jobs.documents.values()))
        self.assertEqual(job['request']['display_name'], 'Новые иконки')
        self.assertIn('в очереди', self.session.last[999].text)
        job.update(status='completed', result={'count': 5})
        with patch.object(handlers, 'render_view', AsyncMock()):
            await handlers.render_status(self.session.last[999], self.state(), self.service,
                                         SimpleNamespace(image_group_repository=self.groups), job)
        self.groups.sync_groups.assert_awaited_once()
        self.assertIsNone(await self.state().get_state())

    async def test_ordinary_and_revoked_admin_cannot_submit(self):
        await self.send('/import_pack', actor=3)
        self.assertIsNone(await self.state(3).get_state())
        await self.send('/import_pack', actor=2)
        await self.send(filename='icons.zip', actor=2)
        self.people[2].status = UserStatus.USER
        await self.send('Название', actor=2)
        self.assertEqual(self.session.downloads, 0)
        self.assertEqual(len(self.jobs.documents), 0)

    async def test_bad_metadata_invalid_name_and_cancel_do_not_download(self):
        await self.send('/import_pack')
        await self.send(filename='icons.rar')
        self.assertEqual(await self.state().get_state(), handlers.PackImportStates.waiting_for_archive.state)
        await self.send(filename='icons.zip', size=self.service.archive_limit + 1)
        self.assertEqual(self.session.downloads, 0)
        await self.send(filename='icons.zip')
        await self.send(' ')
        self.assertEqual(self.session.downloads, 0)
        await self.send('/cancel')
        self.assertIsNone(await self.state().get_state())
        self.assertNotIn('pack_import_document', await self.state().get_data())

    async def test_ambiguous_submission_retry_preserves_original_name_and_archive(self):
        await self.send('/import_pack')
        await self.send(filename='icons.zip')
        original = self.service.submit
        with patch.object(self.service, 'submit', AsyncMock(side_effect=ImportSubmissionUncertain('pending'))):
            await self.send('Первое название')
        self.assertEqual(self.session.downloads, 1)
        self.assertIn('уточняется', self.session.last[999].text)
        await self.send('Другое название')
        self.assertEqual(self.session.downloads, 1)
        self.assertEqual(next(iter(self.jobs.documents.values()))['request']['display_name'], 'Первое название')

    async def test_disk_shortage_stops_before_get_file(self):
        message = SimpleNamespace(bot=SimpleNamespace(get_file=AsyncMock()))
        with patch.object(handlers.shutil, 'disk_usage', return_value=SimpleNamespace(free=0)):
            with self.assertRaises(ValueError):
                await handlers._download(message, {'file_size': 1, 'file_id': 'zip', 'import_id': 'id'}, self.service)
        message.bot.get_file.assert_not_awaited()

    async def test_local_get_file_path_cannot_escape_shared_cache(self):
        message = SimpleNamespace(bot=SimpleNamespace(
            get_file=AsyncMock(return_value=SimpleNamespace(file_size=1, file_path='outside.zip')),
            download_file=AsyncMock(),
        ))
        with patch.object(handlers, 'settings', SimpleNamespace(telegram_api_local=True)):
            with patch.object(handlers.shutil, 'disk_usage', return_value=SimpleNamespace(free=10**12)):
                with self.assertRaises(ValueError):
                    await handlers._download(message, {'file_size': 1, 'file_id': 'zip', 'import_id': 'id'}, self.service)
        message.bot.download_file.assert_not_awaited()

    async def test_missing_get_file_path_produces_expected_error_without_download(self):
        message = SimpleNamespace(bot=SimpleNamespace(
            get_file=AsyncMock(return_value=SimpleNamespace(file_size=1, file_path=None)),
            download_file=AsyncMock(),
        ))
        with patch.object(handlers, 'settings', SimpleNamespace(telegram_api_local=True)):
            with patch.object(handlers.shutil, 'disk_usage', return_value=SimpleNamespace(free=10**12)):
                with self.assertRaisesRegex(ValueError, 'ещё не подготовил архив'):
                    await handlers._download(message, {'file_size': 1, 'file_id': 'zip', 'import_id': 'id'}, self.service)
        message.bot.download_file.assert_not_awaited()
