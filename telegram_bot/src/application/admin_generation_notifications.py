"""Durable admin notices, independent of admission and result delivery.

Each recipient is reserved in Mongo before Telegram I/O. An unknown send outcome
is terminal; only Telegram's explicit FloodWait can schedule another attempt.
"""
import asyncio
import re
from datetime import datetime, timedelta, timezone

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import LinkPreviewOptions

from imager_config.loader import config
from src.utils.loggers import exception_logger, service_logger


def _label(value, fallback):
    if not isinstance(value, str):
        return fallback
    value = ' '.join(''.join(character for character in value if character.isprintable()).split())
    return value[:config.bot.ui_name_max_length] or fallback


def generation_notice_text(notice, actor, group):
    """Use frozen generation values, with bounded, plain-text display labels."""
    parameters = notice['document']['request']['parameters']
    name = _label(getattr(actor, 'name', None), 'Пользователь')
    username = getattr(actor, 'telegram_username', None)
    if (isinstance(username, str) and len(username) <= config.bot.ui_label_max_length
            and re.fullmatch(r'[A-Za-z0-9_]+', username)):
        name += f' (@{username})'
    pack = _label(getattr(group, 'display_name', None), parameters['group_name'])
    return (
        '👀 Изображение принято в очередь\n'
        f"От: {name}\nTelegram ID: {notice['actor_id']}\n"
        f'Пак: {pack}\n'
        f"Качество: {parameters['result_size']} ячеек\n"
        f"Ячейка: {parameters['cell_size']} пикс.; шум: {parameters['noise_level']}"
    )


class AdminGenerationNotifications:
    def __init__(self, admissions, users, groups, bot, *, fixed_admin_ids=()):
        self.admissions, self.users, self.groups, self.bot = admissions, users, groups, bot
        self.fixed_admin_ids = frozenset(fixed_admin_ids)
        self._telegram_resume_at = 0.0

    async def _recipients(self):
        admins = await self.users.get_admins()
        return sorted(self.fixed_admin_ids | {
            admin.telegram_id for admin in admins if admin.is_admin and not admin.isbanned
        })

    async def _can_receive(self, admin_id):
        if admin_id in self.fixed_admin_ids:
            return True
        actor = await self.users.get_user_by_id(admin_id)
        return bool(actor and actor.is_admin and not actor.isbanned)

    async def deliver_once(self):
        loop = asyncio.get_running_loop()
        if loop.time() < self._telegram_resume_at:
            return False
        notice = await self.admissions.next_admin_notice()
        if notice is None:
            return False
        job_id = notice['job_id']
        try:
            recipients = notice['admin_notice']['recipients']
            if recipients is None:
                await self.admissions.initialize_admin_notice(job_id, await self._recipients())
                # Read the persisted snapshot: a competing initializer may win.
                return True
            pending = [(int(key), item) for key, item in recipients.items() if item['state'] == 'pending']
            if not pending:
                return True
            actor = await self.users.get_user_by_id(notice['actor_id'])
            group = await self.groups.get_group_by_name(
                notice['document']['request']['parameters']['group_name'],
            )
            text = generation_notice_text(notice, actor, group)
            for admin_id, receipt in pending:
                if receipt['next_attempt_at'] > datetime.now(timezone.utc):
                    continue
                if not await self._can_receive(admin_id):
                    await self.admissions.skip_admin_notice_recipient(job_id, admin_id)
                    continue
                if not await self.admissions.claim_admin_notice_recipient(job_id, admin_id):
                    continue
                try:
                    message = await self.bot.send_message(
                        chat_id=admin_id, text=text, parse_mode=None,
                        link_preview_options=LinkPreviewOptions(is_disabled=True),
                    )
                except asyncio.CancelledError:
                    raise
                except TelegramRetryAfter as error:
                    delay = max(error.retry_after, 0) + config.bot.notification_retry_after_grace_seconds
                    self._telegram_resume_at = loop.time() + delay
                    await self.admissions.update_admin_notice_recipient(
                        job_id, admin_id, state='pending', error=error,
                        retry_at=datetime.now(timezone.utc) + timedelta(seconds=delay),
                    )
                    exception_logger.warning(f'Admin generation notice rate limited: job_id={job_id}, admin_id={admin_id}')
                    break
                except (TelegramBadRequest, TelegramForbiddenError) as error:
                    exception_logger.exception(f'Admin generation notice rejected: job_id={job_id}, admin_id={admin_id}')
                    await self.admissions.update_admin_notice_recipient(
                        job_id, admin_id, state='failed', error=error,
                    )
                except Exception as error:
                    # The durable reservation already says uncertain. No resend
                    # even if saving the diagnostic fails after this exception.
                    exception_logger.exception(f'Admin generation notice outcome unknown: job_id={job_id}, admin_id={admin_id}')
                    await self.admissions.update_admin_notice_recipient(
                        job_id, admin_id, state='uncertain', error=error,
                    )
                else:
                    if not await self.admissions.update_admin_notice_recipient(
                        job_id, admin_id, state='sent', message_id=message.message_id,
                    ):
                        raise RuntimeError('Cannot acknowledge admin generation notice')
                    service_logger.info(f'Admin generation notice sent: job_id={job_id}, admin_id={admin_id}')
        finally:
            await self.admissions.complete_admin_notice(job_id)
        return True

    async def run(self, stop):
        while not stop.is_set():
            try:
                await self.deliver_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                exception_logger.exception('Admin generation notification polling failed')
            # Admin notices have lower priority than menus and image delivery.
            # Do not drain a burst of accepted images into Telegram at once.
            try:
                await asyncio.wait_for(stop.wait(), timeout=config.bot.notification_poll_seconds)
            except asyncio.TimeoutError:
                pass
