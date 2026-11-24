"""Disposable generation-history projection; storage errors are never empty data."""
from datetime import datetime
from typing import Optional

from bson import ObjectId
from pymongo.asynchronous.collection import AsyncCollection
from pymongo.errors import DuplicateKeyError

from imager_config.loader import config
from settings import settings
from src.models.generation_history import GenerationHistoryRecord, generation_history_record_id


class GenerationHistoryRepository:
    def __init__(self, collection: AsyncCollection):
        self.collection = collection

    async def initialize_indexes(self):
        await self.collection.create_index(
            'job_id', unique=True, partialFilterExpression={'job_id': {'$type': 'string'}},
        )
        await self.collection.create_index([('telegram_id', 1), ('created_at', -1)])
        # The projection is disposable; old records expire instead of growing forever.
        await self.collection.create_index(
            'created_at', name='generation_history_ttl',
            expireAfterSeconds=config.bot.history_retention_days * 24 * 3600,
        )

    async def create_record(self, record: GenerationHistoryRecord) -> str:
        document = record.model_dump()
        if record.job_id:
            record_id = generation_history_record_id(record.job_id)
            document['_id'] = ObjectId(record_id)
            try:
                await self.collection.update_one(
                    {'_id': document['_id']}, {'$setOnInsert': document}, upsert=True,
                )
            except DuplicateKeyError:
                stored = await self.collection.find_one({'_id': document['_id']})
                if stored is None or stored.get('job_id') != record.job_id:
                    raise
            return record_id
        result = await self.collection.insert_one(document)
        return str(result.inserted_id)

    async def count_user_records(self, telegram_id: int) -> int:
        return await self.collection.count_documents({'telegram_id': telegram_id})

    async def count_active_records(self) -> int:
        return await self.collection.count_documents({'status': {'$in': ['queued', 'processing']}})

    async def update_record(
        self, record_id: str, *, status: str, result_image_path: Optional[str] = None,
        error_message: Optional[str] = None, queue_position: Optional[int] = None,
    ) -> bool:
        fields = {'status': status, 'updated_at': datetime.now(settings.tzinfo)}
        if result_image_path is not None:
            fields['result_image_path'] = result_image_path
        if error_message is not None:
            fields['error_message'] = error_message
        if queue_position is not None:
            fields['queue_position'] = queue_position
        result = await self.collection.update_one({'_id': ObjectId(record_id)}, {'$set': fields})
        return bool(result.matched_count)
