"""Journaled support inbox, reply bindings and one-attempt recipient deliveries."""
import asyncio
import math
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from pymongo.errors import DuplicateKeyError
from pymongo.write_concern import WriteConcern

from imager_config.loader import config
from .config import support_config


CHANNELS = frozenset({'support_copy', 'outbound_reply'})


class SupportQueueFull(ValueError):
    pass


class SupportRateLimited(ValueError):
    """A durable accepted-message limit; failed attempts do not consume it."""
    def __init__(self, retry_at, *, now):
        self.retry_at = retry_at
        self.retry_after_seconds = max(1, math.ceil((retry_at - now).total_seconds()))
        super().__init__('Слишком часто. Подождите немного перед следующим сообщением.')


def positive_id(value):
    # Mongo decodes Telegram identities above 2**31 as BSON Int64 (an int
    # subclass). Canonicalize that representation while still rejecting bool.
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError('Support identities must be positive integers')
    return int(value)


class SupportRepository:
    def __init__(self, collection, *, bot_id, policy=support_config):
        self.collection = collection.with_options(write_concern=WriteConcern(w=1, j=True))
        self.bot_id, self.policy = positive_id(bot_id), policy
        self._acceptance_lock = asyncio.Lock()
        self.timezone = ZoneInfo(config.bot.timezone)

    async def ensure_indexes(self):
        await self.collection.create_index('expires_at', expireAfterSeconds=0)
        await self.collection.create_index([('bot_id', 1), ('kind', 1), ('created_at', 1)])
        await self.collection.create_index([('bot_id', 1), ('kind', 1), ('user_chat_id', 1)])
        await self.collection.create_index([
            ('bot_id', 1), ('kind', 1), ('user_chat_id', 1), ('created_at', -1),
        ])

    @staticmethod
    def _now(now=None):
        value = now or datetime.now(timezone.utc)
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError('Support timestamps must have a timezone')
        return value

    def _expiry(self, now):
        return now + timedelta(seconds=self.policy.retention_seconds)

    def _payload(self, payload):
        allowed = {'chat_id', 'message_id', 'user_id', 'name', 'username', 'text', 'content_type'}
        if not isinstance(payload, dict) or set(payload) - allowed:
            raise ValueError('Unsupported support message payload')
        result = deepcopy(payload)
        for name in ('chat_id', 'message_id', 'user_id'):
            result[name] = positive_id(result.get(name))
        if result['chat_id'] != result['user_id']:
            raise ValueError('Support is available in private chats only')
        for name, limit in (('name', self.policy.name_max_length),
                            ('username', self.policy.name_max_length),
                            ('text', self.policy.max_text_length), ('content_type', self.policy.name_max_length)):
            value = result.get(name)
            if value is not None and (not isinstance(value, str) or len(value) > limit):
                raise ValueError(f'Support {name} exceeds its display limit')
        return result

    def _event_id(self, kind, chat_id, message_id):
        return f'{kind}:{self.bot_id}:{positive_id(chat_id)}:{positive_id(message_id)}'

    @staticmethod
    def _pending(now):
        return {'state': 'pending', 'next_attempt_at': now}

    async def activate_admin(self, admin_id, now=None):
        admin_id, moment = positive_id(admin_id), self._now(now)
        await self.collection.update_one({'_id': f'active:{self.bot_id}:{admin_id}'}, {'$set': {
            'kind': 'activation', 'bot_id': self.bot_id, 'admin_id': admin_id, 'updated_at': moment,
        }}, upsert=True)

    async def active_admin_ids(self):
        cursor = self.collection.find({'bot_id': self.bot_id, 'kind': 'activation'}, {'admin_id': 1})
        return [int(row['admin_id']) for row in await cursor.to_list(length=None)]

    async def pending_count(self, user_id=None):
        scope = {'bot_id': self.bot_id, 'kind': 'incoming', 'expires_at': {'$gt': self._now()}}
        if user_id is not None:
            scope['user_chat_id'] = positive_id(user_id)
        cursor = await self.collection.aggregate([
            {'$match': scope},
            {'$project': {'recipients': {'$objectToArray': '$deliveries'}}},
            {'$unwind': '$recipients'},
            {'$match': {'recipients.v.support_copy.state': 'pending'}},
            {'$group': {'_id': '$_id'}}, {'$count': 'total'},
        ])
        rows = await cursor.to_list(length=1)
        return rows[0]['total'] if rows else 0

    async def stored_count(self):
        return await self.collection.count_documents({
            'bot_id': self.bot_id, 'kind': {'$in': ['incoming', 'reply']},
            'expires_at': {'$gt': self._now()},
        })

    async def _check_storage(self, *, reserve_replies=False):
        limit = self.policy.max_stored_events
        if reserve_replies:
            limit -= self.policy.max_pending_total
        if await self.stored_count() >= limit:
            raise SupportQueueFull('Поддержка сейчас занята. Попробуйте написать немного позже.')

    async def _check_message_rate(self, user_id, moment):
        # Only committed incoming records count. This history survives process
        # replacement, successful delivery and administrator replies. The same
        # single-process acceptance lock covers these reads and the insert.
        scope = {'bot_id': self.bot_id, 'kind': 'incoming', 'user_chat_id': positive_id(user_id),
                 'expires_at': {'$gt': moment}}
        latest = await self.collection.find_one(scope, {'created_at': 1}, sort=[('created_at', -1)])
        retries = []
        if latest is not None:
            due = latest['created_at'] + timedelta(seconds=self.policy.message_cooldown_seconds)
            if due > moment:
                retries.append(due)
        window = dict(scope, created_at={'$gt': moment - timedelta(seconds=self.policy.rate_window_seconds)})
        if await self.collection.count_documents(window) >= self.policy.max_messages_per_window:
            first = await self.collection.find_one(window, {'created_at': 1}, sort=[('created_at', 1)])
            retries.append(first['created_at'] + timedelta(seconds=self.policy.rate_window_seconds))
        local_day = moment.astimezone(self.timezone).date()
        start = datetime.combine(local_day, datetime.min.time(), self.timezone)
        end = datetime.combine(local_day + timedelta(days=1), datetime.min.time(), self.timezone)
        day = dict(scope, created_at={'$gte': start, '$lt': end})
        if await self.collection.count_documents(day) >= self.policy.max_messages_per_day:
            retries.append(end)
        if retries:
            raise SupportRateLimited(max(retries), now=moment)

    async def accept_user_message(self, payload, recipient_ids, now=None):
        payload, moment = self._payload(payload), self._now(now)
        identity = self._event_id('in', payload['chat_id'], payload['message_id'])
        recipients = {str(positive_id(identifier)) for identifier in recipient_ids}
        async with self._acceptance_lock:
            if await self.collection.find_one({'_id': identity}, {'_id': 1}):
                return False
            if await self.pending_count(payload['user_id']) >= self.policy.max_pending_per_user:
                raise SupportQueueFull('Слишком много сообщений в очереди. Подождите немного.')
            if await self.pending_count() >= self.policy.max_pending_total:
                raise SupportQueueFull('Поддержка сейчас занята. Попробуйте написать немного позже.')
            await self._check_storage(reserve_replies=True)
            await self._check_message_rate(payload['user_id'], moment)
            row = {'_id': identity, 'bot_id': self.bot_id, 'kind': 'incoming', 'payload': payload,
                   'user_chat_id': payload['chat_id'], 'source_message_id': payload['message_id'],
                   'created_at': moment, 'expires_at': self._expiry(moment),
                   'deliveries': {key: {'support_copy': self._pending(moment)}
                                  for key in sorted(recipients)}}
            return await self._insert(row)

    async def _insert(self, row):
        try:
            result = await self.collection.update_one({'_id': row['_id']}, {'$setOnInsert': row}, upsert=True)
        except DuplicateKeyError:
            return False
        return result.upserted_id is not None

    async def get_event(self, event_id):
        return await self.collection.find_one({'_id': event_id, 'bot_id': self.bot_id,
                                               'kind': {'$in': ['incoming', 'reply']},
                                               'expires_at': {'$gt': self._now()}})

    async def next_delivery(self, channel, active_admin_ids=None, now=None):
        if channel not in CHANNELS:
            raise ValueError('Unknown support delivery channel')
        query = {f'recipients.v.{channel}.state': 'pending',
                 f'recipients.v.{channel}.next_attempt_at': {'$lte': self._now(now)}}
        if active_admin_ids is not None:
            query['recipients.k'] = {'$in': [str(positive_id(identifier)) for identifier in active_admin_ids]}
        cursor = await self.collection.aggregate([
            {'$match': {'bot_id': self.bot_id, 'kind': {'$in': ['incoming', 'reply']},
                        'expires_at': {'$gt': self._now(now)}}},
            {'$set': {'recipients': {'$objectToArray': '$deliveries'}}}, {'$unwind': '$recipients'},
            {'$match': query}, {'$sort': {'created_at': 1, '_id': 1, 'recipients.k': 1}}, {'$limit': 1},
        ])
        rows = await cursor.to_list(length=1)
        if not rows:
            return None
        row = rows[0]
        recipient = int(row.pop('recipients')['k'])
        return {'event_id': row['_id'], 'channel': channel, 'recipient_id': recipient, 'event': row}

    def _delivery_filter(self, event_id, recipient_id, channel, state):
        if channel not in CHANNELS:
            raise ValueError('Unknown support delivery channel')
        path = f'deliveries.{positive_id(recipient_id)}.{channel}'
        return {'_id': event_id, 'bot_id': self.bot_id, f'{path}.state': state}, path

    async def reserve_delivery(self, event_id, recipient_id, channel, now=None):
        moment = self._now(now)
        query, path = self._delivery_filter(event_id, recipient_id, channel, 'pending')
        query[f'{path}.next_attempt_at'] = {'$lte': moment}
        query['expires_at'] = {'$gt': moment}
        result = await self.collection.update_one(query, {'$set': {
            f'{path}.state': 'uncertain', f'{path}.attempted_at': moment,
        }})
        return bool(result.matched_count)

    async def finish_delivery(self, event_id, recipient_id, channel, *, state,
                              message_id=None, error=None, retry_at=None, now=None):
        if state not in {'sent', 'failed', 'skipped', 'uncertain', 'pending'}:
            raise ValueError('Unsupported support delivery result')
        if state == 'pending' and retry_at is None:
            raise ValueError('A definite Telegram rejection requires a retry time')
        query, path = self._delivery_filter(event_id, recipient_id, channel, 'uncertain')
        values = {f'{path}.state': state, f'{path}.updated_at': self._now(now)}
        if message_id is not None:
            values[f'{path}.message_id'] = positive_id(message_id)
        if error is not None:
            values[f'{path}.error'] = str(error)[:self.policy.error_max_length]
        if retry_at is not None:
            if state != 'pending':
                raise ValueError('Only a definite Telegram rejection can be retried')
            values[f'{path}.next_attempt_at'] = self._now(retry_at)
        result = await self.collection.update_one(query, {'$set': values})
        return bool(result.matched_count)

    async def skip_delivery(self, event_id, recipient_id, channel, now=None):
        query, path = self._delivery_filter(event_id, recipient_id, channel, 'pending')
        result = await self.collection.update_one(query, {'$set': {
            f'{path}.state': 'skipped', f'{path}.updated_at': self._now(now),
        }})
        return bool(result.matched_count)

    async def bind_admin_message(self, admin_id, admin_message_id, user_chat_id,
                                 source_message_id, event_id, now=None):
        moment = self._now(now)
        identity = self._event_id('map', admin_id, admin_message_id)
        target = {'user_chat_id': positive_id(user_chat_id), 'source_message_id': positive_id(source_message_id),
                  'event_id': event_id}
        await self._insert({'_id': identity, 'bot_id': self.bot_id, 'kind': 'mapping',
                            'target': target, 'created_at': moment, 'expires_at': self._expiry(moment)})
        stored = await self.collection.find_one({'_id': identity, 'bot_id': self.bot_id})
        if stored is None or stored.get('target') != target:
            raise ValueError('Support reply binding conflicts with an existing conversation')

    async def get_reply_target(self, admin_id, reply_message_id):
        admin_id, reply_message_id = positive_id(admin_id), positive_id(reply_message_id)
        row = await self.collection.find_one({
            '_id': self._event_id('map', admin_id, reply_message_id), 'bot_id': self.bot_id,
            'kind': 'mapping', 'expires_at': {'$gt': self._now()},
        })
        if row:
            target = deepcopy(row['target'])
        else:
            # A known Telegram success is stored before its disposable mapping.
            # If the projection write failed, the receipt still identifies the
            # correct private conversation without repeating the forward.
            event = await self.collection.find_one({
                'bot_id': self.bot_id, 'kind': 'incoming', 'expires_at': {'$gt': self._now()},
                f'deliveries.{admin_id}.support_copy.state': 'sent',
                f'deliveries.{admin_id}.support_copy.message_id': reply_message_id,
            })
            if event is None:
                return None
            target = {'user_chat_id': event['user_chat_id'],
                      'source_message_id': event['source_message_id'], 'event_id': event['_id']}
        target['user_chat_id'] = positive_id(target['user_chat_id'])
        target['source_message_id'] = positive_id(target['source_message_id'])
        return target

    async def accept_admin_reply(self, payload, target, now=None):
        payload, moment = self._payload(payload), self._now(now)
        if (not isinstance(target, dict) or set(target) != {'user_chat_id', 'source_message_id', 'event_id'}
                or target['event_id'] != self._event_id('in', target['user_chat_id'], target['source_message_id'])):
            raise ValueError('Support reply target is not an owned incoming message')
        user_id = positive_id(target['user_chat_id'])
        identity = self._event_id('reply', payload['chat_id'], payload['message_id'])
        async with self._acceptance_lock:
            if await self.collection.find_one({'_id': identity}, {'_id': 1}):
                return False
            await self._check_storage()
            return await self._insert({
                '_id': identity, 'bot_id': self.bot_id, 'kind': 'reply', 'admin_id': payload['user_id'],
                'payload': payload, 'user_chat_id': user_id, 'source_message_id': payload['message_id'],
                'target': deepcopy(target), 'created_at': moment, 'expires_at': self._expiry(moment),
                'deliveries': {str(user_id): {'outbound_reply': self._pending(moment)}},
            })
