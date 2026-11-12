"""Admin uploads a ZIP, names it and observes an isolated import workflow."""
import tempfile
import shutil
from contextlib import nullcontext
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5

from aiogram import F
from aiogram.filters import Command
from aiogram.fsm.state import State, StatesGroup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from imager_config.loader import config
from imager_contracts import ObjectRef
from imager_contracts.catalog_import import CatalogImportRequest
from src.commands.common_keyboards import add_home_button
from src.commands.image_generation.upload import BoundedWriter
from src.infrastructure.services.catalog_import_service import ImportSubmissionUncertain
from src.models.image_build_params import validate_display_name
from src.utils.filters.request_filters.admin_filter import AdminFilter
from src.utils.loggers import exception_logger
from src.utils.navigation import render_view
from settings import settings
from ..ui import AdminCallback, authorize_change, command


class PackImportStates(StatesGroup):
    waiting_for_archive = State()
    waiting_for_name = State()


STATUS_LABELS = {'queued': 'в очереди', 'running': 'выполняется', 'completed': 'завершён', 'failed': 'ошибка'}
PHASE_LABELS = {
    'downloading': 'скачивание архива', 'validating_archive': 'проверка архива',
    'extracting': 'распаковка', 'unpacking': 'распаковка',
    'validating_images': 'проверка изображений', 'validating': 'проверка изображений',
    'uploading': 'загрузка в MinIO',
    'publishing': 'публикация пака', 'completed': 'готово',
}
IMPORT_ID_NAMESPACE = 'imager:telegram:catalog-import:'


def import_id_for(message):
    return str(uuid5(NAMESPACE_URL, f'{IMPORT_ID_NAMESPACE}{message.chat.id}:{message.message_id}'))


def keyboard(buttons=()):
    builder = InlineKeyboardBuilder()
    for title, action in buttons:
        builder.button(text=str(title)[:config.bot.ui_label_max_length], callback_data=action)
    add_home_button(builder)
    builder.adjust(1)
    return builder.as_markup()


async def start_import(event, state, catalog_import_service):
    if getattr(event, 'data', '').startswith('adm:'):
        if await command(event, state) is None:
            return
    elif getattr(event, 'data', None) is not None:
        await event.answer()
    await state.clear()
    await state.set_state(PackImportStates.waiting_for_archive)
    await render_view(event, state,
        f'Отправьте ZIP с изображениями как файл. Максимальный размер: {catalog_import_service.archive_limit // 1_000_000} МБ.\n'
        'Затем задайте название пака. Архив проверит отдельный исполнитель импорта.',
        keyboard([('Мои импорты', 'pack_import:list')]),
    )


async def receive_archive(message, state, catalog_import_service):
    document = message.document
    if document is None or not (document.file_name or '').lower().endswith('.zip'):
        await message.answer('Отправьте ZIP-архив как файл с расширением .zip.')
        return
    if document.file_size is not None and not 0 < document.file_size <= catalog_import_service.archive_limit:
        await message.answer('Архив пуст или превышает допустимый размер ZIP.')
        return
    import_id = import_id_for(message)
    try:
        await catalog_import_service.ensure_capacity(message.from_user.id, import_id)
    except ValueError as error:
        await message.answer(str(error))
        return
    await state.update_data(pack_import_document={
        'file_id': document.file_id, 'file_size': document.file_size,
        'file_name': document.file_name, 'import_id': import_id,
    })
    await state.set_state(PackImportStates.waiting_for_name)
    await render_view(message, state, 'Введите название нового пака:', keyboard())


async def _download(message, descriptor, service):
    required = (descriptor.get('file_size') or service.archive_limit) + config.pack_import.disk_headroom_bytes
    if shutil.disk_usage(tempfile.gettempdir()).free < required:
        raise ValueError('Недостаточно свободного места для загрузки архива. Попробуйте позже.')
    local = getattr(settings, 'telegram_api_local', False)
    cache_root = Path(config.pack_import.bot_api_container_directory).resolve()
    if local and shutil.disk_usage(cache_root).free < required:
        raise ValueError('Недостаточно свободного места в хранилище Telegram. Попробуйте позже.')
    remote = await message.bot.get_file(descriptor['file_id'], request_timeout=config.pack_import.download_timeout_seconds)
    if remote.file_size is not None and remote.file_size > service.archive_limit:
        raise ValueError('ZIP превышает допустимый размер.')
    if not isinstance(remote.file_path, str) or not remote.file_path:
        raise ValueError('Telegram ещё не подготовил архив для скачивания. Повторите ввод названия позже.')
    if local and not Path(remote.file_path).resolve().is_relative_to(cache_root):
        raise ValueError('Telegram вернул недопустимый путь к архиву.')
    # getFile may itself have filled the Local Bot API cache on the same disk.
    required = (remote.file_size or service.archive_limit) + config.pack_import.disk_headroom_bytes
    if shutil.disk_usage(tempfile.gettempdir()).free < required:
        raise ValueError('Недостаточно свободного места для копирования архива. Попробуйте позже.')
    with tempfile.TemporaryDirectory(prefix=config.pack_import.temp_prefix) as directory:
        path = Path(directory) / config.pack_import.archive_filename
        with path.open('wb+') as stream:
            await message.bot.download_file(
                remote.file_path, BoundedWriter(stream, service.archive_limit),
                timeout=config.pack_import.download_timeout_seconds,
            )
        return await service.upload_archive(path, descriptor['import_id'])


async def receive_name(message, state, user_repository, catalog_import_service, uow):
    try:
        name = validate_display_name(message.text)
        if any(ord(char) == 127 for char in name):
            raise ValueError('Название не должно содержать управляющие символы.')
        try:
            name.encode('utf-8')
        except UnicodeEncodeError as error:
            raise ValueError('Название должно содержать корректный текст.') from error
    except ValueError as error:
        await message.answer(str(error))
        return
    data = await state.get_data()
    descriptor = data['pack_import_document']
    import_id = descriptor['import_id']
    existing = await catalog_import_service.get_owned(import_id, message.from_user.id)
    if existing is not None:
        await render_status(message, state, catalog_import_service, uow, existing)
        return
    snapshot = data.get('pack_import_submission')
    try:
        await authorize_change(message, user_repository)
        await catalog_import_service.ensure_capacity(message.from_user.id, import_id)
        if snapshot is None:
            gate = catalog_import_service.upload_gate
            async with gate.acquire(message.from_user.id) if gate is not None else nullcontext():
                reference = data.get('pack_import_reference') or await _download(message, descriptor, catalog_import_service)
            await state.update_data(pack_import_reference=reference)
            request = CatalogImportRequest(
                import_id=import_id, actor_id=message.from_user.id,
                group_name=f'pack_{UUID(import_id).hex}', display_name=name,
                source=ObjectRef.from_dict(reference),
            )
            snapshot = catalog_import_service.prepare(request)
            await state.update_data(pack_import_submission=snapshot)
        await authorize_change(message, user_repository)
        accepted = await catalog_import_service.submit(snapshot)
    except ImportSubmissionUncertain:
        await render_view(message, state,
            f'Приём импорта {import_id} уточняется. Нажмите проверку статуса или повторите ввод названия. '
            'Повтор использует тот же архив и запрос.',
            keyboard([('Проверить статус', f'pack_import:status:{import_id}')]),
        )
        return
    except ValueError as error:
        await render_view(message, state, str(error), keyboard([('Мои импорты', 'pack_import:list')]))
        return
    await render_status(message, state, catalog_import_service, uow, accepted)


async def render_status(event, state, service, uow, job):
    status = job['status']
    request = job['request']
    progress = job.get('progress') or {}
    text = f'Пак: {request["display_name"]}\nИмпорт: {job["_id"]}\nСтатус: {STATUS_LABELS.get(status, "уточняется")}.'
    if status == 'running':
        phase = PHASE_LABELS.get(progress.get('phase'))
        if phase:
            text += f'\nЭтап: {phase}.'
        if progress.get('total'):
            text += f'\nОбработано: {progress.get("processed", 0)} из {progress["total"]}.'
    if status == 'completed':
        text += f'\nИзображений: {(job.get("result") or {}).get("count", 0)}.'
        try:
            groups = await service.core_gateway.list_groups()
            await uow.image_group_repository.sync_groups(groups.groups)
            text += '\nПак доступен в списке библиотек.'
        except Exception:
            exception_logger.exception(f'Cannot synchronize completed import in bot: import_id={job["_id"]}')
            text += '\nСписок библиотек пока не обновлён. Обновите статус позже.'
    elif status == 'failed':
        error = job.get('public_error') or 'Импорт не выполнен. Проверьте архив и попробуйте снова.'
        text += f'\n{error[:config.bot.error_excerpt_length]}'
    await state.clear()
    await render_view(event, state, text, keyboard([
        ('Обновить статус', f'pack_import:status:{job["_id"]}'),
        ('Мои импорты', 'pack_import:list'), ('Загрузить другой пак', 'pack_import:start'),
    ]))


async def status_import(callback, state, catalog_import_service, uow):
    await callback.answer()
    import_id = callback.data.split(':', 2)[2]
    job = await catalog_import_service.get_owned(import_id, callback.from_user.id)
    if job is None:
        snapshot = (await state.get_data()).get('pack_import_submission')
        if snapshot and snapshot.get('_id') == import_id:
            await authorize_change(callback, uow.user_repository)
            job = await catalog_import_service.submit(snapshot)
    if job is None:
        await render_view(callback, state, 'Импорт не найден.', keyboard([('Мои импорты', 'pack_import:list')]))
        return
    await render_status(callback, state, catalog_import_service, uow, job)


async def _render_recent(callback, state, catalog_import_service):
    rows = await catalog_import_service.recent(callback.from_user.id)
    buttons = [(f'{row["request"]["display_name"][:config.bot.ui_label_max_length]} — {STATUS_LABELS.get(row["status"], "уточняется")}',
                f'pack_import:status:{row["_id"]}') for row in rows]
    buttons.append(('Загрузить новый пак', 'pack_import:start'))
    await state.clear()
    await render_view(callback, state, 'Ваши последние импорты:' if rows else 'У вас пока нет импортов.', keyboard(buttons))


async def recent_imports(callback, state, catalog_import_service):
    await callback.answer()
    await _render_recent(callback, state, catalog_import_service)


async def admin_recent_imports(callback, state, catalog_import_service):
    if await command(callback, state) is not None:
        await _render_recent(callback, state, catalog_import_service)


async def require_text_name(message):
    await message.answer('Введите название пака текстом. Для замены архива используйте /cancel.')


def register_pack_import_command(dp):
    dp.message.register(start_import, Command('import_pack'), AdminFilter())


def register_handlers_pack_import(dp, *, with_command=True):
    if with_command:
        register_pack_import_command(dp)
    dp.callback_query.register(start_import, AdminCallback.filter(F.action == 'pack_import'), AdminFilter())
    dp.callback_query.register(start_import, F.data == 'pack_import:start', AdminFilter())
    dp.callback_query.register(status_import, F.data.startswith('pack_import:status:'), AdminFilter())
    dp.callback_query.register(recent_imports, F.data == 'pack_import:list', AdminFilter())
    dp.callback_query.register(admin_recent_imports, AdminCallback.filter(F.action == 'pack_imports'), AdminFilter())
    dp.message.register(receive_archive, PackImportStates.waiting_for_archive, AdminFilter())
    dp.message.register(receive_name, F.text, ~F.text.startswith('/'), PackImportStates.waiting_for_name, AdminFilter())
    dp.message.register(require_text_name, PackImportStates.waiting_for_name, AdminFilter())
