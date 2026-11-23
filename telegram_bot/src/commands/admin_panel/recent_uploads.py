"""Explicit, private administrator access to retained generation sources."""
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from aiogram import F
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import FSInputFile, InaccessibleMessage, InputMediaDocument, InputMediaPhoto

from imager_config.loader import config
from imager_contracts.protocol import (
    BINARY_CONTENT_TYPE, JPEG_CONTENT_TYPE, JOB_ID_PATTERN, PNG_CONTENT_TYPE,
    WEBP_CONTENT_TYPE,
)
from settings import settings
from src.utils.filters.request_filters.admin_filter import AdminFilter
from src.utils.loggers import exception_logger
from .ui import AdminCallback, authorize_change, command, show


MAX_UPLOADS = 10
PHOTO_MAX_BYTES = 10_000_000
PREVIEW_KEY = '_recent_upload_preview'
UNAVAILABLE_TEXT = 'Фото больше недоступно. Нажмите «Обновить», чтобы получить актуальный список.'
EXTENSIONS = {
    JPEG_CONTENT_TYPE: '.jpg', PNG_CONTENT_TYPE: '.png',
    WEBP_CONTENT_TYPE: '.webp', BINARY_CONTENT_TYPE: '.bin',
}


def _safe_label(value, fallback='Пользователь'):
    if not isinstance(value, str):
        return fallback
    text = ' '.join(''.join(char for char in value if char.isprintable()).split())
    return text[:48] or fallback


def _timestamp(value):
    if not isinstance(value, datetime):
        return datetime.min.replace(tzinfo=timezone.utc)
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _format_date(value):
    if not isinstance(value, datetime):
        return 'Дата неизвестна'
    return _timestamp(value).astimezone(settings.tzinfo).strftime('%d.%m.%Y %H:%M')


async def _can_open(callback, user_repository):
    message = callback.message
    if (message is None or isinstance(message, InaccessibleMessage)
            or message.chat.type != 'private'
            or message.chat.id != callback.from_user.id):
        await callback.answer('Фото доступны только в личном чате с ботом.', show_alert=True)
        return False
    try:
        await authorize_change(callback, user_repository)
    except ValueError:
        await callback.answer('Недостаточно прав для просмотра фото.', show_alert=True)
        return False
    return True


async def _access_revoked(callback, state):
    await state.clear()
    await callback.message.edit_text('Недостаточно прав для просмотра фото. Откройте /menu.')


async def _snapshots(rows, user_repository):
    if not isinstance(rows, list):
        raise ValueError('Invalid recent uploads response')
    rows = [row for row in rows if isinstance(row, dict)
            and isinstance(row.get('_id'), str)
            and re.fullmatch(JOB_ID_PATTERN, row['_id'])]
    rows.sort(key=lambda row: (_timestamp(row.get('created_at')), row['_id']), reverse=True)
    snapshots, actors = [], {}
    for row in rows[:MAX_UPLOADS]:
        request = row.get('request') or {}
        context = request.get('client_context') or {}
        actor_id = context.get('user_id')
        actor_id = (int(actor_id) if isinstance(actor_id, int)
                    and not isinstance(actor_id, bool) and actor_id > 0 else None)
        actor = None
        if actor_id is not None:
            if actor_id not in actors:
                actors[actor_id] = await user_repository.get_user_by_id(actor_id)
            actor = actors[actor_id]
        name = _safe_label(getattr(actor, 'name', None))
        username = getattr(actor, 'telegram_username', None)
        if isinstance(username, str) and re.fullmatch(r'[A-Za-z0-9_]{1,32}', username):
            name += f' (@{username})'
        identity = f'{name}, ID {actor_id}' if actor_id is not None else name
        source = request.get('source') or {}
        content_type = source.get('content_type', BINARY_CONTENT_TYPE)
        snapshots.append({
            'job_id': row['_id'], 'date': _format_date(row.get('created_at')),
            'person': identity,
            'content_type': content_type if content_type in EXTENSIONS else BINARY_CONTENT_TYPE,
        })
    return snapshots


async def _render_list(callback, state, snapshots, *, note=None, failed=False):
    text = f'Последние 10 фото\nПринятые изображения: {len(snapshots)} / {MAX_UPLOADS}'
    if snapshots:
        text += '\n\n' + '\n'.join(
            f"{index + 1}. {item['date']} — {item['person']}"
            for index, item in enumerate(snapshots)
        )
    elif not failed:
        text += '\n\nПока нет загруженных фото.'
    if note:
        text += '\n\n' + note
    buttons = [(f'Открыть фото №{index + 1}', 'upload_show', index)
               for index in range(len(snapshots))]
    buttons.extend([('Обновить', 'uploads_refresh', 0), ('Назад', 'home', 0)])
    await show(callback, state, text, buttons,
               records={str(index): {'job_id': item['job_id']}
                        for index, item in enumerate(snapshots)},
               context={'uploads': snapshots})


async def list_recent_uploads(callback, state, recent_uploads_gateway, user_repository):
    if not await _can_open(callback, user_repository):
        return
    action = await command(callback, state)
    if action is None:
        return
    try:
        rows = await recent_uploads_gateway.recent_uploads(limit=MAX_UPLOADS)
        snapshots = await _snapshots(rows, user_repository)
    except Exception:
        exception_logger.exception('Cannot read recent source images for administrator')
        await _render_list(callback, state, [], failed=True,
                           note='Не удалось загрузить список. Нажмите «Обновить» или попробуйте позже.')
        return
    try:
        await authorize_change(callback, user_repository)
    except ValueError:
        await _access_revoked(callback, state)
        return
    await _render_list(callback, state, snapshots)


async def _send_preview(callback, state, path, selected):
    size = path.stat().st_size
    if not 0 < size <= config.limits.source_file_bytes:
        raise ValueError('Invalid downloaded source size')
    content_type = selected['content_type']
    filename = 'source-' + selected['job_id'] + EXTENSIONS[content_type]
    upload = FSInputFile(path, filename=filename)
    caption = f"{selected['date']}\n{selected['person']}"
    as_photo = content_type in (JPEG_CONTENT_TYPE, PNG_CONTENT_TYPE) and size <= PHOTO_MAX_BYTES
    preview = (await state.get_data()).get(PREVIEW_KEY, {})
    if (isinstance(preview, dict) and preview.get('chat_id') == callback.from_user.id
            and type(preview.get('message_id')) is int and preview['message_id'] > 0):
        media_class = InputMediaPhoto if as_photo else InputMediaDocument
        try:
            await callback.bot.edit_message_media(
                chat_id=callback.from_user.id, message_id=preview['message_id'],
                media=media_class(media=upload, caption=caption, parse_mode=None),
            )
        except TelegramBadRequest:
            # A later explicit click may create a new preview if this one was
            # deleted. Never retry or send a second message in this attempt.
            await state.update_data(**{PREVIEW_KEY: None})
            raise
    else:
        if as_photo:
            sent = await callback.bot.send_photo(
                chat_id=callback.from_user.id, photo=upload, caption=caption, parse_mode=None,
            )
        else:
            sent = await callback.bot.send_document(
                chat_id=callback.from_user.id, document=upload, caption=caption, parse_mode=None,
            )
        await state.update_data(**{PREVIEW_KEY: {
            'chat_id': callback.from_user.id, 'message_id': sent.message_id,
        }})


async def show_recent_upload(callback, state, recent_uploads_gateway, user_repository):
    if not await _can_open(callback, user_repository):
        return
    action = await command(callback, state)
    if action is None:
        return
    snapshots = action.context.get('uploads', [])
    if (not isinstance(snapshots, list) or not 0 <= action.item < len(snapshots)
            or action.record.get('job_id') != snapshots[action.item].get('job_id')):
        await _render_list(callback, state, [], note=UNAVAILABLE_TEXT, failed=True)
        return
    selected = snapshots[action.item]
    # Consume this screen's nonce before any download/send. A queued double tap
    # cannot send the same source again after the per-user isolation is released.
    await _render_list(callback, state, snapshots, note=f'Открываем фото №{action.item + 1}…')
    with tempfile.TemporaryDirectory(prefix='imager-admin-source-') as directory:
        path = Path(directory) / 'source.bin'
        try:
            await recent_uploads_gateway.download_source(selected['job_id'], path)
        except Exception:
            exception_logger.exception(f"Cannot fetch administrator source preview: job_id={selected['job_id']}")
            await _render_list(callback, state, snapshots, note=UNAVAILABLE_TEXT)
            return
        try:
            await authorize_change(callback, user_repository)
        except ValueError:
            await _access_revoked(callback, state)
            return
        try:
            await _send_preview(callback, state, path, selected)
        except Exception:
            exception_logger.exception(f"Cannot send administrator source preview: job_id={selected['job_id']}")
            await _render_list(callback, state, snapshots,
                               note='Не удалось показать фото. Обновите список или откройте фото ещё раз.')
            return
    await _render_list(callback, state, snapshots, note=f'Фото №{action.item + 1} открыто ниже.')


def register_handlers_recent_uploads(dp):
    dp.callback_query.register(list_recent_uploads,
                               AdminCallback.filter(F.action.in_({'uploads', 'uploads_refresh'})),
                               AdminFilter())
    dp.callback_query.register(show_recent_upload, AdminCallback.filter(F.action == 'upload_show'),
                               AdminFilter())
