from motor.motor_asyncio import AsyncIOMotorClient
from settings import settings
from src.infrastructure.data.unit_of_work import get_uow


def setup_database():
    mongo_client = AsyncIOMotorClient(settings.mongodb_uri)
    db = mongo_client.get_database(settings.database_name)
    uow = get_uow(db)
    return uow
