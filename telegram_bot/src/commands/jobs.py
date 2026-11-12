"""Show an owner's image progress without exposing internal execution details."""
import re

from aiogram import F
from aiogram.filters import Command, CommandObject
from aiogram.utils.keyboard import InlineKeyboardBuilder
from pymongo.errors import PyMongoError

from imager_contracts.protocol import JOB_ID_PATTERN
from src.commands.common_keyboards import add_home_button, home_keyboard
from src.utils.loggers import exception_logger
from src.utils.navigation import render_view


UNKNOWN_JOB_TEXT = 'Не удалось найти изображение. Вернитесь в главное меню.'
NO_IMAGE_TEXT = 'Вы ещё не отправляли фото для обработки. Нажмите «Собрать изображение» в главном меню.'
PENDING_IMAGE_TEXT = 'Фото принято. Уточняю статус обработки. Результат придёт в этот чат.'
FAILED_ADMISSION_TEXT = 'Не удалось начать обработку изображения. Попробуйте отправить фото ещё раз через главное меню.'
MISSING_RESULT_TEXT = 'Не удалось получить готовое изображение. Попробуйте позже.'
IMAGE_PROGRESS = {
    'queued': 'Фото принято. Изображение ожидает обработки. Результат придёт в этот чат.',
    'running': 'Собираю изображение. Когда оно будет готово, пришлю его сюда.',
    'retrying': 'Обработка заняла больше времени. Продолжаю собирать изображение.',
    'failed': 'Не удалось собрать изображение. Попробуйте выбрать другие параметры в главном меню.',
}


def valid_job_id(value):
    return isinstance(value, str) and re.fullmatch(JOB_ID_PATTERN, value) is not None


def _admission_text(receipt):
    return FAILED_ADMISSION_TEXT if receipt.state == 'failed' else PENDING_IMAGE_TEXT


async def _render(event, state, text, keyboard=None):
    await state.clear()
    await render_view(event, state, text, keyboard or home_keyboard())


def _owned(job, actor_id):
    request = job.get('request') or {}
    return request.get('client_id') == 'telegram' and (request.get('client_context') or {}).get('user_id') == actor_id


async def _load_owned(job_id, actor_id, generation_service):
    receipt = await generation_service.admissions.find(job_id)
    if receipt is not None and receipt.actor_id != actor_id:
        return None, None
    try:
        job = await generation_service.gateway.get_job(job_id)
    except (PyMongoError, OSError, TimeoutError):
        if receipt is None:
            raise
        exception_logger.exception(f'Core job status temporarily unavailable: job_id={job_id}')
        return receipt, None
    if job is not None and not _owned(job, actor_id):
        return None, None
    return receipt, job


async def _latest_id(actor_id, generation_service):
    receipt = await generation_service.admissions.latest(actor_id)
    if receipt is not None:
        return receipt.job_id if receipt.actor_id == actor_id else None
    job = await generation_service.gateway.latest_user_job(actor_id)
    return job['_id'] if job is not None and _owned(job, actor_id) else None


async def show_latest(event, state, user, generation_service):
    job_id = await _latest_id(user.telegram_id, generation_service)
    if job_id is None:
        await _render(event, state, NO_IMAGE_TEXT)
        return
    await show_status(event, state, user, generation_service, job_id)


async def show_status(event, state, user, generation_service, job_id, *, notice=None):
    if not valid_job_id(job_id):
        await _render(event, state, UNKNOWN_JOB_TEXT)
        return
    receipt, job = await _load_owned(job_id, user.telegram_id, generation_service)
    if receipt is None and job is None:
        await _render(event, state, UNKNOWN_JOB_TEXT)
        return
    builder = InlineKeyboardBuilder()
    builder.button(text='Обновить статус', callback_data=f'job_status:{job_id}')
    text = _admission_text(receipt) if receipt is not None else PENDING_IMAGE_TEXT
    if job is not None:
        outbox = job.get('completion_outbox') or {}
        if job['status'] == 'completed' and not job.get('result'):
            text = MISSING_RESULT_TEXT
        elif job['status'] == 'completed':
            text = {
                'sent': 'Изображение готово и уже отправлено в этот чат.',
                'failed': 'Изображение готово, но отправить его пока не удалось. Нажмите «Получить изображение», чтобы попробовать ещё раз.',
            }.get(outbox.get('state'), 'Изображение готово. Отправляю его в этот чат.')
            if job.get('result') and outbox.get('state') == 'failed':
                builder.button(text='Получить изображение', callback_data=f'job_retry:{job_id}')
        else:
            text = IMAGE_PROGRESS.get(job['status'], PENDING_IMAGE_TEXT)
    add_home_button(builder)
    builder.adjust(1)
    await _render(event, state, f'{notice}\n\n{text}' if notice else text, builder.as_markup())


async def retry_delivery(event, state, user, generation_service, job_id):
    if not valid_job_id(job_id):
        await _render(event, state, UNKNOWN_JOB_TEXT)
        return
    receipt, job = await _load_owned(job_id, user.telegram_id, generation_service)
    if job is None:
        await _render(event, state, UNKNOWN_JOB_TEXT if receipt is None else _admission_text(receipt))
        return
    changed = await generation_service.gateway.retry_delivery(job_id, user.telegram_id)
    notice = ('Отправлю готовое изображение ещё раз. Дневной лимит не изменится.'
              if changed else 'Повторная отправка сейчас недоступна. Проверьте статус изображения.')
    await show_status(event, state, user, generation_service, job_id, notice=notice)


async def status_command(message, state, user, generation_service, command: CommandObject):
    legacy_id = (command.args or '').strip()
    if legacy_id:
        await show_status(message, state, user, generation_service, legacy_id)
    else:
        await show_latest(message, state, user, generation_service)


async def retry_command(message, state, user, generation_service, command: CommandObject):
    legacy_id = (command.args or '').strip()
    job_id = legacy_id or await _latest_id(user.telegram_id, generation_service)
    if job_id is None:
        await _render(message, state, NO_IMAGE_TEXT)
        return
    await retry_delivery(message, state, user, generation_service, job_id)


async def latest_callback(callback, state, user, generation_service):
    await callback.answer()
    await show_latest(callback, state, user, generation_service)


async def status_callback(callback, state, user, generation_service):
    await callback.answer()
    await show_status(callback, state, user, generation_service, callback.data.split(':', 1)[1])


async def retry_callback(callback, state, user, generation_service):
    await callback.answer()
    await retry_delivery(callback, state, user, generation_service, callback.data.split(':', 1)[1])


def register_handlers_jobs(dp):
    dp.message.register(status_command, Command('status'))
    dp.message.register(retry_command, Command('retry_delivery'))
    dp.callback_query.register(latest_callback, F.data == 'my_image')
    dp.callback_query.register(status_callback, F.data.startswith('job_status:'))
    dp.callback_query.register(retry_callback, F.data.startswith('job_retry:'))
