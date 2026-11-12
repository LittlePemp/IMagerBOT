"""Owners can inspect an accepted job or retry delivery without another render."""
import re

from aiogram import F
from aiogram.filters import Command, CommandObject
from aiogram.utils.keyboard import InlineKeyboardBuilder
from pymongo.errors import PyMongoError

from imager_contracts.protocol import JOB_ID_PATTERN
from src.commands.common_keyboards import add_home_button, home_keyboard
from src.utils.loggers import exception_logger
from src.utils.navigation import render_view


JOB_LABELS = {
    'queued': 'в очереди', 'running': 'выполняется', 'retrying': 'ожидает повторной попытки',
    'completed': 'готово', 'failed': 'ошибка сборки',
}
DELIVERY_LABELS = {
    'pending': 'ожидает отправки', 'claimed': 'отправляется',
    'sent': 'отправлено', 'failed': 'не доставлено',
}
ADMISSION_LABELS = {
    'pending': 'ожидает передачи ядру', 'claimed': 'передаётся ядру',
    'sent': 'передано ядру', 'failed': 'передача требует проверки',
}
UNKNOWN_JOB_TEXT = 'Задание не найдено или принадлежит другому пользователю.'


def valid_job_id(value):
    return isinstance(value, str) and re.fullmatch(JOB_ID_PATTERN, value) is not None


async def _render(event, state, text, keyboard=None):
    await state.clear()
    await render_view(event, state, text, keyboard or home_keyboard())


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
    if job is not None and (
        job['request']['client_id'] != 'telegram'
        or job['request']['client_context']['user_id'] != actor_id
    ):
        return None, None
    return receipt, job


async def show_status(event, state, user, generation_service, job_id, *, notice=None):
    if not valid_job_id(job_id):
        await _render(event, state, 'Укажите корректный номер задания: /status НОМЕР_ЗАДАНИЯ')
        return
    receipt, job = await _load_owned(job_id, user.telegram_id, generation_service)
    if receipt is None and job is None:
        await _render(event, state, UNKNOWN_JOB_TEXT)
        return
    builder = InlineKeyboardBuilder()
    builder.button(text='Обновить статус', callback_data=f'job_status:{job_id}')
    if job is None:
        status = ADMISSION_LABELS.get(receipt.state, 'статус уточняется')
        text = f'Задание: {job_id}\nПриём: {status}.\nСборка: статус уточняется.\nДоставка: ожидает готовности.'
    else:
        outbox = job.get('completion_outbox') or {}
        text = (
            f'Задание: {job_id}\nСборка: {JOB_LABELS.get(job["status"], "статус уточняется")}.\n'
            f'Доставка: {DELIVERY_LABELS.get(outbox.get("state"), "ожидает готовности")}.'
        )
        if job['status'] == 'completed' and job.get('result') and outbox.get('state') == 'failed':
            builder.button(text='Повторить доставку', callback_data=f'job_retry:{job_id}')
    add_home_button(builder)
    builder.adjust(1)
    await _render(event, state, f'{notice}\n\n{text}' if notice else text, builder.as_markup())


async def retry_delivery(event, state, user, generation_service, job_id):
    if not valid_job_id(job_id):
        await _render(event, state, 'Укажите корректный номер задания: /retry_delivery НОМЕР_ЗАДАНИЯ')
        return
    receipt, job = await _load_owned(job_id, user.telegram_id, generation_service)
    if job is None:
        await _render(event, state, UNKNOWN_JOB_TEXT if receipt is None else (
            f'Задание {job_id} принято. Статус сборки уточняется; повтор доставки пока недоступен.'
        ))
        return
    changed = await generation_service.gateway.retry_delivery(job_id, user.telegram_id)
    notice = ('Повторная доставка запланирована. Новая сборка не запускается, дневной лимит не расходуется.'
              if changed else 'Повтор сейчас недоступен: доставка уже ожидается, выполняется или завершена.')
    await show_status(event, state, user, generation_service, job_id, notice=notice)


async def status_command(message, state, user, generation_service, command: CommandObject):
    await show_status(message, state, user, generation_service, (command.args or '').strip())


async def retry_command(message, state, user, generation_service, command: CommandObject):
    await retry_delivery(message, state, user, generation_service, (command.args or '').strip())


async def status_callback(callback, state, user, generation_service):
    await callback.answer()
    await show_status(callback, state, user, generation_service, callback.data.split(':', 1)[1])


async def retry_callback(callback, state, user, generation_service):
    await callback.answer()
    await retry_delivery(callback, state, user, generation_service, callback.data.split(':', 1)[1])


def register_handlers_jobs(dp):
    dp.message.register(status_command, Command('status'))
    dp.message.register(retry_command, Command('retry_delivery'))
    dp.callback_query.register(status_callback, F.data.startswith('job_status:'))
    dp.callback_query.register(retry_callback, F.data.startswith('job_retry:'))
