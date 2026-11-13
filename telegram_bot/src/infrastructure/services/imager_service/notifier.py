"""Delivery of core completion-outbox events to Telegram.

Each result has one send attempt. A durable boundary before Telegram I/O prevents a
timeout or a process restart from causing another document to be sent.
"""
import asyncio
import tempfile
from contextlib import suppress
from pathlib import Path

from aiogram.exceptions import TelegramRetryAfter
from aiogram.types import FSInputFile

from imager_config.loader import config
from src.models.generation_history import GenerationHistoryRecord, generation_history_record_id
from src.utils.loggers import exception_logger, service_logger
from src.utils.result_files import delivery_result_filename


class GenerationNotifier:
    def __init__(self, service, bot, history_repository, *, progress=None, on_result_delivered=None):
        self.service = service
        self.bot = bot
        self.history_repository = history_repository
        self.progress = progress
        self.on_result_delivered = on_result_delivered
        self._telegram_resume_at = 0.0

    async def _renew(self, job_id, token, lost):
        interval = max(
            config.bot.notification_renew_min_seconds,
            self.service.config.bot_notification_lease_seconds / config.bot.notification_renew_divisor,
        )
        while True:
            await asyncio.sleep(interval)
            try:
                if not await self.service.renew_completion(job_id, token):
                    lost.set()
                    return
            except Exception:
                exception_logger.exception(f'Cannot renew Telegram delivery: job_id={job_id}')
                lost.set()
                return

    async def deliver_once(self):
        if asyncio.get_running_loop().time() < self._telegram_resume_at:
            return False
        job = await self.service.claim_completion()
        if job is None:
            return False
        job_id = job['_id']
        outbox = job['completion_outbox']
        token = outbox['token']
        lost = asyncio.Event()
        renewal = asyncio.create_task(self._renew(job_id, token, lost))
        send_started = False
        accepted_message_id = None
        try:
            context = job['request']['client_context']
            chat_id = context['chat_id']
            result = job.get('result')
            if job['status'] == 'completed':
                if not result:
                    raise ValueError('Completed job has no result object')
                with tempfile.TemporaryDirectory(prefix=config.storage.result_temp_prefix) as directory:
                    path = Path(directory) / config.storage.result_filename
                    await asyncio.to_thread(
                        self.service.object_store.download_result, result, path,
                        max_bytes=self.service.config.telegram_max_document_size_bytes,
                    )
                    filename = delivery_result_filename(
                        context.get('display_name'), created_at=job.get('created_at'), job_id=job_id,
                    )
                    document = FSInputFile(path, filename=filename)
                    if lost.is_set() or not await self.service.mark_delivery_started(job_id, token):
                        raise RuntimeError('Delivery lease was lost before sending the document')
                    send_started = True
                    message = await self.bot.send_document(
                        chat_id=chat_id,
                        document=document,
                        caption='Ваше изображение готово.',
                    )
                    accepted_message_id = message.message_id
            else:
                if lost.is_set() or not await self.service.mark_delivery_started(job_id, token):
                    raise RuntimeError('Delivery lease was lost before sending the notification')
                send_started = True
                if self.progress is not None:
                    accepted_message_id = await self.progress.fail_generation(job)
                if accepted_message_id is None:
                    message = await self.bot.send_message(
                        chat_id=chat_id,
                        text='😔 Не получилось собрать изображение.',
                    )
                    accepted_message_id = message.message_id
            acknowledged = await self.service.acknowledge_completion(
                job_id, token, accepted_message_id
            )
            if not acknowledged:
                raise RuntimeError(
                    'Telegram accepted the notification but its acknowledgement lease was lost'
                )
            service_logger.info(f'Telegram result delivered: job_id={job_id}')
        except asyncio.CancelledError:
            # Only preparation is reclaimable. An interrupted send stays
            # uncertain after expiry and must never be sent again.
            raise
        except Exception as error:
            if isinstance(error, TelegramRetryAfter):
                # A conservative process-wide pause also protects other chats
                # while Telegram applies a bot-wide FloodWait.
                self._telegram_resume_at = asyncio.get_running_loop().time() + (
                    max(error.retry_after, 0) + config.bot.notification_retry_after_grace_seconds
                )
            action = 'acknowledgement' if accepted_message_id is not None else 'delivery'
            exception_logger.exception(f'Telegram result {action} failed: job_id={job_id}')
            if accepted_message_id is None:
                await self.service.fail_completion(
                    job_id, token, error, send_started=send_started,
                )
            # Telegram's known success must not become a false delivery failure
            # when Mongo is unavailable. Its started claim cannot be resent.
        finally:
            renewal.cancel()
            with suppress(asyncio.CancelledError):
                await renewal
        if (job['status'] == 'completed' and accepted_message_id is not None
                and self.on_result_delivered is not None):
            try:
                await self.on_result_delivered(job)
            except asyncio.CancelledError:
                raise
            except Exception:
                # Menu delivery never changes the already sent result outbox.
                exception_logger.exception(f'Cannot show menu after image delivery: job_id={job_id}')
        return True

    async def repair_history_once(self):
        """History is a disposable projection, never a prerequisite for sending."""
        job = await self.service.claim_history_projection()
        if job is None:
            return False
        job_id = job['_id']
        projection = job['telegram_history_projection']
        token = projection['token']
        try:
            context = job['request']['client_context']
            history_id = context['history_record_id']
            if history_id != generation_history_record_id(job_id):
                raise ValueError('Accepted job has an inconsistent history identity')
            ensured = await self.history_repository.create_record(GenerationHistoryRecord.from_job(job))
            if ensured != history_id:
                raise RuntimeError('Cannot restore generation history projection')
            result = job.get('result')
            updated = await self.history_repository.update_record(
                history_id,
                status=job['status'],
                result_image_path=f"s3://{result['bucket']}/{result['key']}" if result else None,
                error_message=str(job.get('error') or '') if job['status'] == 'failed' else None,
            )
            if not updated:
                raise RuntimeError('Cannot update generation history projection')
            if not await self.service.acknowledge_history_projection(job_id, token):
                raise RuntimeError('History projection lease was lost before acknowledgement')
        except asyncio.CancelledError:
            raise
        except Exception as error:
            exception_logger.exception(f'Generation history repair failed: job_id={job_id}')
            await self.service.fail_history_projection(job_id, token, projection['attempts'], error)
        return True

    @staticmethod
    async def _wait(stop, seconds):
        try:
            await asyncio.wait_for(stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    async def _delivery_loop(self, stop):
        while not stop.is_set():
            try:
                if await self.deliver_once():
                    continue
            except asyncio.CancelledError:
                raise
            except Exception:
                exception_logger.exception('Telegram completion inbox polling failed')
            await self._wait(stop, self.service.config.bot_notification_poll_seconds)

    async def _history_loop(self, stop):
        while not stop.is_set():
            try:
                if await self.repair_history_once():
                    continue
            except asyncio.CancelledError:
                raise
            except Exception:
                exception_logger.exception('Generation history repair polling failed')
            await self._wait(stop, config.bot.history_repair_poll_seconds)

    async def run(self, stop):
        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(self._delivery_loop(stop), name='telegram-delivery')
            tasks.create_task(self._history_loop(stop), name='generation-history-repair')
