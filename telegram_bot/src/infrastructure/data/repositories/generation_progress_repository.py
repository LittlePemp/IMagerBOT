"""Durable UI messages, separate from immutable generation requests."""
import math
from datetime import datetime, timedelta, timezone

from pymongo import ReturnDocument
from pymongo.asynchronous.collection import AsyncCollection
from pymongo.errors import DuplicateKeyError


class GenerationProgressRepository:
    def __init__(self, collection: AsyncCollection, retention_seconds):
        if (type(retention_seconds) not in (int, float)
                or not math.isfinite(retention_seconds) or retention_seconds <= 0):
            raise ValueError('Progress retention must be finite and positive')
        self.collection = collection
        self.retention_seconds = retention_seconds

    async def initialize_indexes(self):
        await self.collection.create_index('expires_at', expireAfterSeconds=0)
        await self.collection.create_index([
            ('finished', 1), ('created_at', 1), ('_id', 1),
        ])

    async def track(self, job_id, *, actor_id, chat_id, message_id, text):
        now = datetime.now(timezone.utc)
        try:
            stored = await self.collection.find_one_and_update(
                {'_id': job_id},
                {'$setOnInsert': {
                    '_id': job_id, 'actor_id': actor_id, 'chat_id': chat_id,
                    'message_id': message_id, 'last_text': text, 'finished': False,
                    'created_at': now, 'updated_at': now,
                }},
                upsert=True, return_document=ReturnDocument.AFTER,
            )
        except DuplicateKeyError:
            # A competing caller may have inserted this exact projection.
            stored = await self.get(job_id)
            if stored is None:
                raise
        if stored is None:
            raise RuntimeError('Progress tracking did not return its stored message')
        if stored['actor_id'] != actor_id or stored['chat_id'] != chat_id:
            raise ValueError('Progress message belongs to another actor or chat')
        return stored

    async def get(self, job_id):
        return await self.collection.find_one({'_id': job_id})

    async def active(self, limit):
        if type(limit) is not int or limit <= 0:
            raise ValueError('Progress batch size must be a positive integer')
        cursor = self.collection.find({'finished': False})
        cursor = cursor.sort([('created_at', 1), ('_id', 1)]).limit(limit)
        return await cursor.to_list(length=limit)

    async def remember(self, job_id, text, *, finished=False):
        now = datetime.now(timezone.utc)
        fields = {'last_text': text, 'updated_at': now}
        if finished:
            fields.update(
                finished=True,
                expires_at=now + timedelta(seconds=self.retention_seconds),
            )
        result = await self.collection.update_one({'_id': job_id}, {'$set': fields})
        return bool(result.matched_count)
