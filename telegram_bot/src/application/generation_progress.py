"""Edit one durable Telegram message while the core builds an image."""
import asyncio
from contextlib import suppress

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import ReactionTypeEmoji

from imager_config.loader import config
from src.utils.loggers import exception_logger


PROCESSING_REACTION = '👀'
ACCEPTING_TEXT = '👀'
QUEUE_TEXT = 'Изображение принято в работу. Вы в очереди: {position}.'
BUILDING_TEXT = '🧩 Собираю изображение…'
RENDERING_TEXT = '🧩 Собираю изображение: {percent}%'
DELIVERING_TEXT = '📤 Отправляю изображение…'
DONE_TEXT = '✅ Готово!'
BUILD_FAILED_TEXT = 'Не получилось собрать изображение 😔'
DELIVERY_FAILED_TEXT = 'Ой, изображение не дошло 😔'
DELIVERY_UNCERTAIN_TEXT = 'Не удалось проверить, дошло ли изображение 😔'
ADMISSION_FAILED_TEXT = 'Не удалось подтвердить приём фото 😔'


class ProgressUpdateUncertain(RuntimeError):
    """An edit may have arrived; do not replace it with another message."""


class GenerationProgress:
    def __init__(self, repository, admissions, gateway, bot):
        self.repository, self.admissions = repository, admissions
        self.gateway, self.bot = gateway, bot
        self._edit_lock = asyncio.Lock()
        self._resume_at = 0.0

    async def _queue(self):
        # Publication may overlap this read. Merge admission and core snapshots
        # by identity so the same image never occupies two queue positions.
        pending = await self.admissions.pending_submissions(config.bot.max_pending_jobs)
        active = await self.gateway.active_queue(pending_ids=[job['_id'] for job in pending])
        jobs = {job['_id']: job for job in pending}
        jobs.update({job['_id']: job for job in active})
        return sorted(
            (job for job in jobs.values() if job['status'] not in ('completed', 'failed')),
            key=lambda job: (job['created_at'], job['_id']),
        )

    @staticmethod
    def _text(job, position):
        if job is None or job['status'] == 'queued':
            return (QUEUE_TEXT.format(position=position), False) if position else (ACCEPTING_TEXT, False)
        if job['status'] in ('running', 'retrying'):
            progress = (job.get('execution') or {}).get('progress') or {}
            completed, total = progress.get('completed_cells'), progress.get('total_cells')
            if (job['status'] == 'running' and progress.get('stage') == 'rendering' and type(completed) is int
                    and type(total) is int and total > 0):
                return RENDERING_TEXT.format(percent=min(100, max(0, completed * 100 // total))), False
            return BUILDING_TEXT, False
        if job['status'] == 'failed':
            return BUILD_FAILED_TEXT, True
        delivery = (job.get('completion_outbox') or {}).get('state')
        if delivery == 'sent':
            return DONE_TEXT, True
        if delivery == 'failed':
            return DELIVERY_FAILED_TEXT, True
        if delivery == 'uncertain':
            return DELIVERY_UNCERTAIN_TEXT, True
        return DELIVERING_TEXT, False

    async def watch(self, message, *, job_id, actor_id, duplicate=False):
        existing = await self.repository.get(job_id)
        if existing:
            if existing['actor_id'] != actor_id or existing['chat_id'] != message.chat.id:
                raise ValueError('Image progress belongs to another account')
            return existing
        job = await self.gateway.get_job(job_id)
        if duplicate and job and job['status'] in ('completed', 'failed'):
            return None
        queue = await self._queue()
        position = next((index for index, item in enumerate(queue, 1) if item['_id'] == job_id), None)
        text, finished = self._text(job, position)
        sent = await message.reply(text)
        record = await self.repository.track(
            job_id, actor_id=actor_id, chat_id=message.chat.id,
            message_id=sent.message_id, text=text,
        )
        if finished:
            await self.repository.remember(job_id, text, finished=True)
        try:
            await self.bot.set_message_reaction(
                chat_id=message.chat.id, message_id=message.message_id,
                reaction=[ReactionTypeEmoji(emoji=PROCESSING_REACTION)], is_big=False,
            )
        except Exception:
            exception_logger.exception(f'Cannot set image reaction: job_id={job_id}')
        return record

    async def _edit(self, record, text, *, finished):
        async with self._edit_lock:
            record = await self.repository.get(record['_id'])
            if record is None:
                return None
            if record.get('finished'):
                return record['message_id'] if record['last_text'] == text else None
            try:
                if record.get('last_text') != text:
                    await self.bot.edit_message_text(
                        chat_id=record['chat_id'], message_id=record['message_id'], text=text,
                    )
            except TelegramRetryAfter as error:
                self._resume_at = asyncio.get_running_loop().time() + (
                    max(error.retry_after, 0) + config.bot.notification_retry_after_grace_seconds
                )
                exception_logger.exception(f'Telegram progress rate limited: job_id={record["_id"]}')
                return None
            except TelegramBadRequest as error:
                if 'message is not modified' not in error.message.lower():
                    exception_logger.exception(f'Cannot edit image progress: job_id={record["_id"]}')
                    await self.repository.remember(record['_id'], record['last_text'], finished=True)
                    return None
            except TelegramForbiddenError:
                exception_logger.exception(f'Image progress chat unavailable: job_id={record["_id"]}')
                await self.repository.remember(record['_id'], record['last_text'], finished=True)
                return None
            except Exception:
                exception_logger.exception(f'Image progress update failed: job_id={record["_id"]}')
                return None
            await self.repository.remember(record['_id'], text, finished=finished)
            return record['message_id']

    async def fail_generation(self, job):
        record = await self.repository.get(job['_id'])
        if record is None:
            return None
        message_id = await self._edit(record, BUILD_FAILED_TEXT, finished=True)
        if message_id is None:
            current = await self.repository.get(job['_id'])
            if current and current.get('finished') and current['last_text'] == BUILD_FAILED_TEXT:
                return current['message_id']
            if current is not None and not current.get('finished'):
                raise ProgressUpdateUncertain('Cannot confirm the generation failure edit')
        return message_id

    async def poll_once(self):
        if asyncio.get_running_loop().time() < self._resume_at:
            return
        records = await self.repository.active(config.bot.progress_batch_size)
        if not records:
            return
        queue = await self._queue()
        positions = {job['_id']: index for index, job in enumerate(queue, 1)}
        jobs = await self.gateway.progress_jobs([record['_id'] for record in records])
        by_id = {job['_id']: job for job in jobs}
        for record in records:
            if asyncio.get_running_loop().time() < self._resume_at:
                break
            job = by_id.get(record['_id'])
            if job is not None:
                context = job['request']['client_context']
                if context.get('user_id') != record['actor_id'] or context.get('chat_id') != record['chat_id']:
                    exception_logger.error(f'Image progress ownership mismatch: job_id={record["_id"]}')
                    await self.repository.remember(record['_id'], ADMISSION_FAILED_TEXT, finished=True)
                    continue
            text, finished = self._text(job, positions.get(record['_id']))
            if job is None and record['_id'] not in positions:
                receipt = await self.admissions.find(record['_id'])
                if receipt is not None and receipt.state == 'sent':
                    # Publication may have finished between the batch snapshot
                    # and the admission read. Never report that as rejection.
                    refreshed = await self.gateway.get_job(record['_id'])
                    if refreshed is None:
                        continue
                    text, finished = self._text(refreshed, positions.get(record['_id']))
                elif receipt is None or receipt.state == 'failed':
                    text, finished = ADMISSION_FAILED_TEXT, True
            await self._edit(record, text, finished=finished)

    async def run(self, stop):
        while not stop.is_set():
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                exception_logger.exception('Image progress polling failed')
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=config.bot.progress_poll_seconds)
