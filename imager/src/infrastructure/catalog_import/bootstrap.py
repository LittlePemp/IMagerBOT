"""Import-worker process resources; no client or image decoder at module import."""
from functools import lru_cache


@lru_cache(maxsize=1)
def create_import_repository():
    from pymongo import MongoClient
    from imager_config.loader import config
    from settings import settings
    from .mongo_imports import MongoImportRepository
    client = MongoClient(
        settings.mongodb_uri, tz_aware=True,
        serverSelectionTimeoutMS=config.mongo.server_selection_timeout_ms,
        connectTimeoutMS=config.mongo.connect_timeout_ms,
        socketTimeoutMS=config.mongo.socket_timeout_ms,
        waitQueueTimeoutMS=config.mongo.wait_queue_timeout_ms,
    )
    database = client[settings.database_name]
    repository = MongoImportRepository(database[config.pack_import.jobs_collection], database[config.mongo.catalog_collection])
    try:
        repository.ensure_indexes()
    except Exception:
        client.close()
        raise
    return repository


@lru_cache(maxsize=1)
def create_import_executor():
    from src.infrastructure.runtime_bootstrap import create_object_store
    from src.application.catalog_import.executor import CatalogImportExecutor
    return CatalogImportExecutor(create_object_store(), create_import_repository())
