"""Support routing across two bot identities without downloading Telegram files."""
import asyncio
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions, ReplyParameters

from imager_config.loader import config
from src.utils.loggers import exception_logger, service_logger
from .config import support_config
from .repository import SupportQueueFull


SUPPORTED_TYPES = frozenset({'text', 'photo', 'document', 'video', 'voice', 'audio',
                             'sticker', 'animation', 'video_note'})
TYPE_LABELS = {'text': 'Текст', 'photo': 'Фото', 'document': 'Файл', 'video': 'Видео',
               'voice': 'Голосовое сообщение', 'audio': 'Аудио', 'sticker': 'Стикер',
               'animation': 'Анимация', 'video_note': 'Видеосообщение'}
CHANNELS = ('outbound_reply', 'support_copy', 'main_alert')
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
    def __init__(self, repository, users, main_bot, support_bot, support_url, *,
                 fixed_admin_ids=(), policy=support_config):
        parsed = urlsplit(support_url)
        if (parsed.scheme != 'https' or parsed.netloc != 't.me' or parsed.query or parsed.fragment
                or re.fullmatch(r'/[A-Za-z0-9_]+', parsed.path) is None):
            raise ValueError('Support requires its own public Telegram bot link')
        if int(support_bot.id) != repository.bot_id:
            raise ValueError('Support repository belongs to another bot')
        self.repository, self.users = repository, users
        self.main_bot, self.support_bot, self.support_url = main_bot, support_bot, support_url
        self.fixed_admin_ids, self.policy = frozenset(fixed_admin_ids), policy
        self._resume_at = {'main': 0.0, 'support': 0.0}
        self._channel_index = 0

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
            await message.answer(STAFF_HELP)
        else:
            await message.answer(CUSTOMER_HELP)

    async def accept_user_message(self, message, user):
        payload = self._payload(message, user)
        if payload is None:
            await message.answer(UNSUPPORTED_TEXT)
            return False
        actor = await self.users.get_user_by_id(user.telegram_id)
        if actor is None or (actor.isbanned and user.telegram_id not in self.fixed_admin_ids):
            raise ValueError('Приём сообщений недоступен для этого аккаунта.')
        try:
            accepted = await self.repository.accept_user_message(payload, await self._recipients())
        except SupportQueueFull as error:
            await message.answer(str(error))
            return False
        if accepted:
            await message.answer('Сообщение принято. Ответ придёт в этот чат.')
        return accepted

    async def accept_admin_reply(self, message, user):
        self._verify_private(message, user)
        if not await self.is_admin(user.telegram_id):
            await message.answer('Доступ к ответам поддержки недоступен для этого аккаунта.')
            return False
        original = message.reply_to_message
        if (original is None or original.from_user is None
                or original.from_user.id != self.support_bot.id or not original.from_user.is_bot):
            await message.answer(REPLY_HELP)
            return False
        target = await self.repository.get_reply_target(user.telegram_id, original.message_id)
        if target is None:
            await message.answer(REPLY_HELP)
            return False
        payload = self._payload(message, user)
        if payload is None:
            await message.answer(UNSUPPORTED_TEXT)
            return False
        try:
            accepted = await self.repository.accept_admin_reply(payload, target)
        except SupportQueueFull as error:
            await message.answer(str(error))
            return False
        if accepted:
            await message.answer('Ответ принят.')
        return accepted

    async def redirect_main_reply(self, message):
        await message.answer(
            'Ответьте пользователю в боте поддержки: откройте его и ответьте на пересланное сообщение.',
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text='Открыть и ответить', url=self.support_url + '?start=staff'),
            ]]),
        )

    def _main_notice(self, event):
        payload = event['payload']
        name = _label(payload.get('name'), 'Пользователь', self.policy.name_max_length)
        username = payload.get('username')
        if isinstance(username, str) and re.fullmatch(r'[A-Za-z0-9_]+', username):
            name += f' (@{username})'
        kind = TYPE_LABELS.get(payload.get('content_type'), 'Сообщение')
        text = (f'📨 Сообщение в поддержку\nОт: {name}\n'
                f"Telegram ID: {payload['user_id']}\nТип: {kind}\n"
                'Ответьте в боте поддержки по кнопке ниже.')
        if payload.get('text'):
            text += '\n\n' + payload['text']
        return _clip_text(text, self.policy.max_text_length)

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
        if channel == 'main_alert':
            return await self.main_bot.send_message(
                chat_id=recipient, text=self._main_notice(event), parse_mode=None,
                link_preview_options=LinkPreviewOptions(is_disabled=True),
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text='Открыть и ответить', url=self.support_url + '?start=staff'),
                ]]),
            )
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
        candidate = None
        for offset in range(len(CHANNELS)):
            index = (self._channel_index + offset) % len(CHANNELS)
            channel = CHANNELS[index]
            bot_kind = 'main' if channel == 'main_alert' else 'support'
            if loop.time() < self._resume_at[bot_kind]:
                continue
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
            bot_kind = 'main' if channel == 'main_alert' else 'support'
            self._resume_at[bot_kind] = loop.time() + delay
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
