"""Process-scoped repository bundle. This object does not claim a transaction.

The shared Mongo client owns connection pools. Each write uses an explicit
single-document atomic operation; there is no mutable per-request session here.
"""
from pymongo.asynchronous.database import AsyncDatabase

from imager_config.loader import config
from src.infrastructure.data.repositories.admission_repository import AdmissionRepository
from src.infrastructure.data.repositories.generation_progress_repository import GenerationProgressRepository
from src.infrastructure.data.repositories.user_repository import UserRepository
from src.infrastructure.data.repositories.generation_history_repository import GenerationHistoryRepository
from src.infrastructure.data.repositories.image_build_repository import ImageBuildParamsRepository
from src.infrastructure.data.repositories.image_group_repository import ImageGroupRepository


class MongoRepositories:
    def __init__(self, db: AsyncDatabase):
        self.db = db
        self.user_repository = UserRepository(db['users'])
        self.generation_history_repository = GenerationHistoryRepository(db['generation_history'])
        self.image_build_params_repository = ImageBuildParamsRepository(db['image_build_params'])
        self.image_group_repository = ImageGroupRepository(db['image_groups'])
        self.admission_repository = AdmissionRepository(
            db[config.mongo.bot_admissions_collection], timezone=config.bot.timezone,
            daily_limit=config.bot.regular_user_upload_limit,
            lease_seconds=config.bot.admission_lease_seconds,
            retry_seconds=config.bot.admission_retry_seconds,
        )
        self.generation_progress_repository = GenerationProgressRepository(
            db[config.mongo.bot_progress_collection],
            retention_seconds=config.bot.progress_retention_seconds,
        )

    async def initialize_indexes(self):
        for repository in (
            self.user_repository, self.generation_history_repository,
            self.image_build_params_repository, self.image_group_repository,
            self.admission_repository, self.generation_progress_repository,
        ):
            await repository.initialize_indexes()

    async def __aenter__(self):
        # Compatibility for callers being migrated from the old misleading UOW.
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        return False


MongoUnitOfWork = MongoRepositories


def get_uow(db):
    return MongoRepositories(db)
