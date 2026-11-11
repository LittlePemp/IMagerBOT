"""Composition root for the container worker and dispatcher.

These resources are process-local singletons. Importing the module does not
connect to Mongo, Temporal or MinIO and does not load the image library.
"""
from functools import lru_cache


@lru_cache(maxsize=1)
def create_job_repository():
    from pymongo import MongoClient
    from settings import settings
    from src.infrastructure.jobs.mongo_jobs import MongoJobRepository
    client = MongoClient(
        settings.mongodb_uri, tz_aware=True, serverSelectionTimeoutMS=10000,
        connectTimeoutMS=5000, socketTimeoutMS=10000, waitQueueTimeoutMS=10000,
    )
    repository = MongoJobRepository(
        client[settings.database_name].generation_jobs,
        lease_seconds=settings.job_execution_lease_seconds,
    )
    repository.ensure_indexes()
    return repository


@lru_cache(maxsize=1)
def create_object_store():
    from settings import settings
    from src.infrastructure.object_storage.minio_store import MinioObjectStore
    store = MinioObjectStore(
        endpoint=settings.minio_endpoint, access_key=settings.minio_access_key.get_secret_value(),
        secret_key=settings.minio_secret_key.get_secret_value(), bucket=settings.minio_bucket,
        secure=settings.minio_secure,
    )
    store.ensure_bucket()
    return store


@lru_cache(maxsize=1)
def create_render_executor():
    from settings import settings
    from src.application.image_builder.services.object_render import ObjectRenderExecutor
    from src.infrastructure.object_storage.catalog import MinioCatalogProvider
    store = create_object_store()
    return ObjectRenderExecutor(
        store, MinioCatalogProvider(store), jpeg_quality=settings.generated_image_jpeg_quality,
        max_output_bytes=settings.telegram_max_document_size_bytes,
    )
