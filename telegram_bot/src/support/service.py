"""Bounded support routing entirely within the support bot."""
import asyncio
import time
from collections import OrderedDict
from datetime import datetime, timedelta, timezone

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import ReplyParameters

from imager_config.loader import config
from src.utils.loggers import exception_logger, service_logger
from .config import support_config
from .repository import SupportQueueFull, SupportRateLimited


SUPPORTED_TYPES = frozenset({'text', 'photo', 'document', 'video', 'voice', 'audio',
                             'sticker', 'animation', 'video_note'})
CHANNELS = ('outbound_reply', 'support_copy')
CUSTOMER_HELP = 'Опишите проблему. Можно приложить фото, видео или файл. Ответ придёт в этот чат.'
STAFF_HELP = ('Поддержка подключена. Здесь появятся сообщения пользователей. '
              'Чтобы ответить пользователю, ответьте на его пересланное сообщение.')
REPLY_HELP = 'Чтобы ответить пользователю, ответьте на его пересланное сообщение в этом чате.'
UNSUPPORTED_TEXT = 'Отправьте текст, фото, видео, голосовое сообщение или файл.'


def _label(value, fallback, limit):
    if not isinstance(value, str):
        return fallback
    return ' '.join(''.join(char for char in value if char.isprintable()).split())[:limit] or fallback


def _clip_text(value, units):
    """Telegram counts message limits in UTF-16 units, including emoji pairs."""
    return value.encode('utf-16-le', errors='replace')[:units * 2].decode('utf-16-le', errors='ignore')


class SupportService:
    def __init__(self, repository, users, support_bot, *,
                 fixed_admin_ids=(), policy=support_config, clock=time.monotonic):
        if int(support_bot.id) != repository.bot_id:
            raise ValueError('Support repository belongs to another bot')
        self.repository, self.users = repository, users
        self.support_bot = support_bot
        self.fixed_admin_ids, self.policy = frozenset(fixed_admin_ids), policy
        self._resume_at = 0.0
        self._channel_index = 0
        self._clock, self._notices = clock, OrderedDict()

    def _notice_allowed(self, actor_id, kind):
        """At most one acknowledgement and one warning per user per minute.

        This cache only bounds explanatory replies. Admission rate limits live
        in Mongo and survive process replacement; evicting this cache cannot
        make another customer message reach an administrator.
        """
        now = self._clock()
        while self._notices and next(iter(self._notices.values())) <= now - config.bot.throttle_idle_seconds:
            self._notices.popitem(last=False)
        key = actor_id, kind
        previous = self._notices.get(key)
        if previous is not None and now - previous < self.policy.notice_cooldown_seconds:
            return False
        if key not in self._notices and len(self._notices) >= config.bot.throttle_user_capacity:
            self._notices.popitem(last=False)
        self._notices[key] = now
        self._notices.move_to_end(key)
        return True

    async def _notice(self, message, user, text, *, kind='warning'):
        if self._notice_allowed(user.telegram_id, kind):
            await message.answer(text)

    async def is_admin(self, identifier):
        if identifier in self.fixed_admin_ids:
            return True
        actor = await self.users.get_user_by_id(identifier)
        return bool(actor and actor.is_admin and not actor.isbanned)

    async def _recipients(self):
        admins = await self.users.get_admins()
        return sorted(self.fixed_admin_ids | {
            actor.telegram_id for actor in admins if actor.is_admin and not actor.isbanned
        })

    @staticmethod
    def _verify_private(message, user):
        if (message.chat.type != 'private' or message.from_user is None
                or message.chat.id != message.from_user.id
                or message.from_user.id != user.telegram_id):
            raise ValueError('Support is available to the sender in a private chat only')

    def _payload(self, message, user):
        self._verify_private(message, user)
        kind = getattr(message.content_type, 'value', message.content_type)
        if kind not in SUPPORTED_TYPES:
            return None
        return {
            'chat_id': message.chat.id, 'message_id': message.message_id,
            'user_id': user.telegram_id,
            'name': _label(getattr(user, 'name', None), 'Пользователь', self.policy.name_max_length),
            'username': _label(getattr(user, 'telegram_username', None), '', self.policy.name_max_length),
            'text': _clip_text(message.text or message.caption or '', self.policy.max_text_length),
            'content_type': kind,
        }

    async def start(self, message, user):
        self._verify_private(message, user)
        if await self.is_admin(user.telegram_id):
            await self.repository.activate_admin(user.telegram_id)
            await self._notice(message, user, STAFF_HELP, kind='help')
        else:
            await self._notice(message, user, CUSTOMER_HELP, kind='help')

    async def accept_user_message(self, message, user):
        payload = self._payload(message, user)
        if payload is None:
            await self._notice(message, user, UNSUPPORTED_TEXT)
            return False
        actor = await self.users.get_user_by_id(user.telegram_id)
        if actor is None or (actor.isbanned and user.telegram_id not in self.fixed_admin_ids):
            raise ValueError('Приём сообщений недоступен для этого аккаунта.')
        try:
            accepted = await self.repository.accept_user_message(payload, await self._recipients())
        except (SupportQueueFull, SupportRateLimited) as error:
            await self._notice(message, user, str(error))
            return False
        if accepted:
            await self._notice(message, user, 'Сообщение принято. Ответ придёт в этот чат.', kind='acknowledgement')
        return accepted

    async def accept_admin_reply(self, message, user):
        self._verify_private(message, user)
        if not await self.is_admin(user.telegram_id):
            await self._notice(message, user, 'Доступ к ответам поддержки недоступен для этого аккаунта.')
            return False
        original = message.reply_to_message
        if (original is None or original.from_user is None
                or original.from_user.id != self.support_bot.id or not original.from_user.is_bot):
            await self._notice(message, user, REPLY_HELP)
            return False
        target = await self.repository.get_reply_target(user.telegram_id, original.message_id)
        if target is None:
            await self._notice(message, user, REPLY_HELP)
            return False
        payload = self._payload(message, user)
        if payload is None:
            await self._notice(message, user, UNSUPPORTED_TEXT)
            return False
        try:
            accepted = await self.repository.accept_admin_reply(payload, target)
        except SupportQueueFull as error:
            await self._notice(message, user, str(error))
            return False
        return accepted

    async def _eligible(self, candidate):
        event, recipient = candidate['event'], candidate['recipient_id']
        if candidate['channel'] == 'outbound_reply':
            if not await self.is_admin(event['admin_id']):
                return False
            user = await self.users.get_user_by_id(recipient)
            return bool(user and (not user.isbanned or recipient in self.fixed_admin_ids))
        return await self.is_admin(recipient)

    async def _send(self, candidate):
        event, recipient, channel = candidate['event'], candidate['recipient_id'], candidate['channel']
        payload = event['payload']
        if channel == 'support_copy':
            # Both source and destination belong to this support bot. Telegram
            # retains the original text, captions and media without a download.
            return await self.support_bot.forward_message(
                chat_id=recipient, from_chat_id=payload['chat_id'], message_id=payload['message_id'],
            )
        return await self.support_bot.copy_message(
            chat_id=recipient, from_chat_id=payload['chat_id'], message_id=payload['message_id'],
            reply_parameters=ReplyParameters(
                message_id=event['target']['source_message_id'], allow_sending_without_reply=True,
            ),
        )

    async def deliver_once(self):
        loop = asyncio.get_running_loop()
        if loop.time() < self._resume_at:
            return False
        candidate = None
        for offset in range(len(CHANNELS)):
            index = (self._channel_index + offset) % len(CHANNELS)
            channel = CHANNELS[index]
            active = await self.repository.active_admin_ids() if channel == 'support_copy' else None
            candidate = await self.repository.next_delivery(channel, active_admin_ids=active)
            if candidate is not None:
                self._channel_index = (index + 1) % len(CHANNELS)
                break
        if candidate is None:
            return False
        event_id, admin_id, channel = candidate['event_id'], candidate['recipient_id'], candidate['channel']
        if not await self._eligible(candidate):
            await self.repository.skip_delivery(event_id, admin_id, channel)
            return True
        if not await self.repository.reserve_delivery(event_id, admin_id, channel):
            return True
        accepted_message_id = None
        delivery_recorded = False
        try:
            message = await self._send(candidate)
            accepted_message_id = message.message_id
            if not await self.repository.finish_delivery(
                event_id, admin_id, channel, state='sent', message_id=accepted_message_id,
            ):
                raise RuntimeError('Cannot acknowledge support delivery')
            delivery_recorded = True
            if channel == 'support_copy':
                await self.repository.bind_admin_message(
                    admin_id, accepted_message_id, candidate['event']['user_chat_id'],
                    candidate['event']['source_message_id'], event_id,
                )
        except asyncio.CancelledError:
            raise
        except TelegramRetryAfter as error:
            delay = max(error.retry_after, 0) + config.bot.notification_retry_after_grace_seconds
            self._resume_at = loop.time() + delay
            await self.repository.finish_delivery(
                event_id, admin_id, channel, state='pending', error=error,
                retry_at=datetime.now(timezone.utc) + timedelta(seconds=delay),
            )
            exception_logger.warning(f'Support delivery rate limited: event_id={event_id}, channel={channel}')
        except (TelegramBadRequest, TelegramForbiddenError) as error:
            exception_logger.exception(f'Support delivery rejected: event_id={event_id}, channel={channel}')
            await self.repository.finish_delivery(event_id, admin_id, channel, state='failed', error=error)
        except Exception as error:
            # Reservation is already uncertain. A lost Telegram response or DB
            # acknowledgement must not trigger another media/message send.
            action = 'reply binding' if delivery_recorded else 'delivery confirmation' if accepted_message_id is not None else 'delivery'
            exception_logger.exception(f'Support {action} failed: event_id={event_id}, channel={channel}')
            if accepted_message_id is None:
                await self.repository.finish_delivery(event_id, admin_id, channel, state='uncertain', error=error)
            elif channel == 'support_copy' and not delivery_recorded:
                # If the receipt acknowledgement failed, still preserve the
                # known message identity in its idempotent reply projection.
                try:
                    await self.repository.bind_admin_message(
                        admin_id, accepted_message_id, candidate['event']['user_chat_id'],
                        candidate['event']['source_message_id'], event_id,
                    )
                except Exception:
                    exception_logger.exception(f'Support reply binding recovery failed: event_id={event_id}')
        else:
            service_logger.info(f'Support delivery sent: event_id={event_id}, channel={channel}')
        return True

    async def run(self, stop):
        while not stop.is_set():
            try:
                await self.deliver_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                exception_logger.exception('Support delivery polling failed')
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.policy.poll_seconds)
            except asyncio.TimeoutError:
                pass
