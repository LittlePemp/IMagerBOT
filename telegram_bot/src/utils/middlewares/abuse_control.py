"""Bound work before database access, and bound expensive transfers separately."""
import asyncio
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from dataclasses import dataclass

from aiogram import BaseMiddleware
from aiogram.exceptions import TelegramAPIError

from imager_config.loader import config
from src.utils.loggers import exception_logger


@dataclass
class Bucket:
    tokens: float
    updated: float
    notice_at: float = 0
    photo_at: float = float('-inf')


class RequestLimiter:
    def __init__(self, policy=config.bot, *, clock=time.monotonic):
        self.policy, self.clock = policy, clock
        self.users = OrderedDict()
        self.global_bucket = Bucket(policy.global_updates_burst, clock())

    @staticmethod
    def _refill(bucket, now, rate, burst):
        bucket.tokens = min(burst, bucket.tokens + (now - bucket.updated) * rate)
        bucket.updated = now

    def check(self, actor_id, *, photo=False):
        now, p = self.clock(), self.policy
        while self.users and next(iter(self.users.values())).updated < now - p.throttle_idle_seconds:
            self.users.popitem(last=False)
        bucket = self.users.get(actor_id)
        if bucket is None:
            if len(self.users) >= p.throttle_user_capacity:
                self.users.popitem(last=False)
            bucket = Bucket(p.updates_burst, now)
        self.users[actor_id] = bucket
        self.users.move_to_end(actor_id)
        self._refill(bucket, now, p.updates_per_second, p.updates_burst)
        self._refill(self.global_bucket, now, p.global_updates_per_second, p.global_updates_burst)
        global_ok = self.global_bucket.tokens >= 1
        allowed = global_ok and bucket.tokens >= 1 and (
            not photo or now - bucket.photo_at >= p.upload_cooldown_seconds
        )
        if allowed:
            bucket.tokens -= 1
            self.global_bucket.tokens -= 1
            if photo:
                bucket.photo_at = now
        notice = not allowed and global_ok and now >= bucket.notice_at
        if notice:
            bucket.notice_at = now + p.throttle_notice_seconds
            self.global_bucket.tokens -= 1
        return allowed, notice

    def notice(self, actor_id):
        now = self.clock()
        bucket = self.users[actor_id]
        if now < bucket.notice_at or self.global_bucket.tokens < 1:
            return False
        bucket.notice_at = now + self.policy.throttle_notice_seconds
        self.global_bucket.tokens -= 1
        return True


class AbuseControlMiddleware(BaseMiddleware):
    def __init__(self, limiter=None):
        self.limiter = limiter or RequestLimiter()
        self.active = set()

    async def __call__(self, handler, update, data):
        event = update.message or update.callback_query
        if event is None or event.from_user is None:
            return
        allowed, notice = self.limiter.check(event.from_user.id, photo=bool(getattr(event, 'photo', None)))
        if not allowed:
            if notice:
                try:
                    await event.answer('Слишком часто. Подождите немного и повторите.')
                except TelegramAPIError:
                    exception_logger.warning('Could not deliver throttling notice')
            return
        message = event.message if update.callback_query else event
        if message is None or message.chat.type != 'private':
            if update.callback_query and self.limiter.notice(event.from_user.id):
                await event.answer('Используйте бота в личном чате.')
            return
        actor_id = event.from_user.id
        if actor_id in self.active:
            if self.limiter.notice(actor_id):
                await event.answer('Предыдущее действие ещё выполняется. Подождите немного.')
            return
        self.active.add(actor_id)
        try:
            return await handler(update, data)
        finally:
            self.active.discard(actor_id)


class UploadBusy(ValueError):
    pass


class UploadGate:
    def __init__(self, capacity=config.bot.max_concurrent_uploads):
        self.capacity = capacity
        self.actors = set()
        self.admission_lock = asyncio.Lock()

    @asynccontextmanager
    async def acquire(self, actor_id):
        if actor_id in self.actors or len(self.actors) >= self.capacity:
            raise UploadBusy('Загрузка уже выполняется. Повторите немного позже.')
        self.actors.add(actor_id)
        try:
            yield
        finally:
            self.actors.discard(actor_id)
