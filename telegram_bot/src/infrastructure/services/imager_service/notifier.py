"""Delivery of core completion-outbox events to Telegram.

Delivery is at least once: a process can stop after Telegram accepts a document
and before Mongo acknowledges the event. The canonical core result is unchanged.
"""
import asyncio
import tempfile
from contextlib import suppress
from pathlib import Path

from aiogram.types import FSInputFile

from imager_config.loader import config
from src.models.generation_history import GenerationHistoryRecord, generation_history_record_id
from src.utils.loggers import exception_logger, service_logger


class GenerationNotifier:
    def __init__(self, service, bot, history_repository):
        self.service = service
        self.bot = bot
        self.history_repository = history_repository

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
        job = await self.service.claim_completion()
        if job is None:
            return False
        job_id = job['_id']
        outbox = job['completion_outbox']
        token = outbox['token']
        context = job['request']['client_context']
        lost = asyncio.Event()
        renewal = asyncio.create_task(self._renew(job_id, token, lost))
        try:
            chat_id = context['chat_id']
            history_id = context.get('history_record_id')
            error = str(job.get('error') or 'Не удалось собрать изображение.')
            result = job.get('result')
            if history_id:
                if history_id != generation_history_record_id(job_id):
                    raise ValueError('Accepted job has an inconsistent history identity')
                ensured = await self.history_repository.create_record(GenerationHistoryRecord.from_job(job))
                if ensured != history_id:
                    raise RuntimeError('Cannot restore generation history before notification')
                updated = await self.history_repository.update_record(
                    history_id,
                    status=job['status'],
                    result_image_path=(
                        f"s3://{result['bucket']}/{result['key']}" if result else None
                    ),
                    error_message=error if job['status'] == 'failed' else None,
                    queue_position=0,
                )
                if not updated:
                    raise RuntimeError('Cannot update generation history before notification')
            if job['status'] == 'completed':
                if not result:
                    raise ValueError('Completed job has no result object')
                with tempfile.TemporaryDirectory(prefix=config.storage.result_temp_prefix) as directory:
                    path = Path(directory) / config.storage.result_filename
                    await asyncio.to_thread(
                        self.service.object_store.download_result, result, path,
                        max_bytes=self.service.config.telegram_max_document_size_bytes,
                    )
                    if lost.is_set():
                        raise RuntimeError('Delivery lease was lost before sending the document')
                    filename = context.get('display_name') or job_id
                    message = await self.bot.send_document(
                        chat_id=chat_id,
                        document=FSInputFile(
                            path, filename=f'{filename}{Path(config.storage.result_filename).suffix}'
                        ),
                        caption=f'Ваше изображение готово.\nЗадание: {job_id}',
                    )
            else:
                if lost.is_set():
                    raise RuntimeError('Delivery lease was lost before sending the notification')
                message = await self.bot.send_message(
                    chat_id=chat_id,
                    text=f'Не удалось собрать изображение.\nЗадание: {job_id}\n{error[:config.bot.error_excerpt_length]}',
                )
            acknowledged = await self.service.acknowledge_completion(
                job_id, token, message.message_id
            )
            if not acknowledged:
                raise RuntimeError(
                    'Telegram accepted the notification but its acknowledgement lease was lost'
                )
            service_logger.info(f'Telegram result delivered: job_id={job_id}')
        except asyncio.CancelledError:
            # A later notifier can reclaim the event after its lease expires.
            raise
        except Exception as error:
            exception_logger.exception(f'Telegram result delivery failed: job_id={job_id}')
            await self.service.fail_completion(job_id, token, outbox['attempts'], error)
        finally:
            renewal.cancel()
            with suppress(asyncio.CancelledError):
                await renewal
        return True

    async def run(self, stop):
        while not stop.is_set():
            try:
                if await self.deliver_once():
                    continue
            except asyncio.CancelledError:
                raise
            except Exception:
                exception_logger.exception('Telegram completion inbox polling failed')
            try:
                await asyncio.wait_for(
                    stop.wait(), timeout=self.service.config.bot_notification_poll_seconds
                )
            except asyncio.TimeoutError:
                pass
