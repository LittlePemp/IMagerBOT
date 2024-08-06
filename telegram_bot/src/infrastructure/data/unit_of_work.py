from motor.motor_asyncio import AsyncIOMotorDatabase
from src.infrastructure.data.repositories.user_repository import UserRepository
from src.utils.loggers import db_logger, exception_logger
from src.infrastructure.data.repositories.image_build_repository import ImageBuildParamsRepository


class MongoUnitOfWork:
    _instance = None

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super(MongoUnitOfWork, cls).__new__(cls)
        return cls._instance

    def __init__(self, db: AsyncIOMotorDatabase):
        if hasattr(self, '_initialized') and self._initialized:
            return
        self.db = db
        self.user_repository = UserRepository(db['users'])
        self.image_build_params_repository = ImageBuildParamsRepository(db['image_build_params'])
        self.session = None
        self._initialized = True

    async def __aenter__(self):
        try:
            self.session = await self.db.client.start_session()
            self.session.start_transaction()
            db_logger.info('Transaction started')
            return self
        except Exception as e:
            exception_logger.error(f'Failed to start transaction: {e}')
            raise

    async def __aexit__(self, exc_type, exc_value, traceback):
        try:
            if exc_type is None:
                await self.session.commit_transaction()
                db_logger.info('Transaction committed')
            else:
                await self.session.abort_transaction()
                exception_logger.error(f'Transaction aborted due to exception: {exc_value}')
            self.session.end_session()
        except Exception as e:
            exception_logger.error(f'Failed to end transaction: {e}')
            raise

    async def commit(self):
        if self.session:
            try:
                await self.session.commit_transaction()
                db_logger.info('Transaction committed')
            except Exception as e:
                exception_logger.error(f'Failed to commit transaction: {e}')
                raise

    async def rollback(self):
        if self.session:
            try:
                await self.session.abort_transaction()
                db_logger.info('Transaction aborted')
            except Exception as e:
                exception_logger.error(f'Failed to abort transaction: {e}')
                raise

def get_uow(db):
    return MongoUnitOfWork(db)
