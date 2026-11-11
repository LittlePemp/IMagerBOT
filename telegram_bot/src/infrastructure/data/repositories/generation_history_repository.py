from datetime import datetime
from typing import Optional

from motor.motor_asyncio import AsyncIOMotorCollection
from pymongo.errors import DuplicateKeyError

from settings import settings
from src.models.generation_history import GenerationHistoryRecord, generation_history_record_id
from src.utils.loggers import db_logger, exception_logger


class GenerationHistoryRepository:
    _instance = None

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super(GenerationHistoryRepository, cls).__new__(cls)
        return cls._instance

    def __init__(self, collection: AsyncIOMotorCollection):
        if hasattr(self, "_initialized") and self._initialized:
            return
        self.collection = collection
        self._initialized = True

    async def create_record(self, record: GenerationHistoryRecord) -> Optional[str]:
        try:
            document = record.model_dump()
            if record.job_id:
                from bson import ObjectId

                record_id = generation_history_record_id(record.job_id)
                document['_id'] = ObjectId(record_id)
                try:
                    await self.collection.update_one(
                        {'_id': document['_id']}, {'$setOnInsert': document}, upsert=True,
                    )
                except DuplicateKeyError:
                    # Concurrent projections of the same accepted command
                    # race on the same immutable Mongo identity.
                    pass
            else:
                result = await self.collection.insert_one(document)
                record_id = str(result.inserted_id)
            db_logger.info(f"Generation history record created: {record_id}")
            return record_id
        except Exception as e:
            exception_logger.error(f"Failed to create generation history record: {e}")
            return None

    async def count_user_records(self, telegram_id: int) -> int:
        try:
            return await self.collection.count_documents({"telegram_id": telegram_id})
        except Exception as e:
            exception_logger.error(f"Failed to count generation history for user {telegram_id}: {e}")
            return 0

    async def count_active_records(self) -> int:
        try:
            return await self.collection.count_documents({"status": {"$in": ["queued", "processing"]}})
        except Exception as e:
            exception_logger.error(f"Failed to count active generation history records: {e}")
            return 0

    async def update_record(
        self,
        record_id: str,
        *,
        status: str,
        result_image_path: Optional[str] = None,
        error_message: Optional[str] = None,
        queue_position: Optional[int] = None,
    ) -> bool:
        try:
            update_data = {
                "status": status,
                "updated_at": datetime.now(settings.tzinfo),
            }
            if result_image_path is not None:
                update_data["result_image_path"] = result_image_path
            if error_message is not None:
                update_data["error_message"] = error_message
            if queue_position is not None:
                update_data["queue_position"] = queue_position

            from bson import ObjectId

            result = await self.collection.update_one(
                {"_id": ObjectId(record_id)},
                {"$set": update_data},
            )
            if result.matched_count:
                db_logger.info(f"Generation history record updated: {record_id}")
                return True
            return False
        except Exception as e:
            exception_logger.error(f"Failed to update generation history record {record_id}: {e}")
            return False
