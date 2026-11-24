"""Composition root for the container worker and dispatcher.

These resources are process-local singletons. Importing the module does not
connect to Mongo, Temporal or MinIO and does not load the image library.
"""
from functools import lru_cache


@lru_cache(maxsize=1)
def create_job_repository():
    from pymongo import MongoClient
    from imager_config.loader import config
    from settings import settings
    from src.infrastructure.jobs.mongo_jobs import MongoJobRepository
    client = MongoClient(
        settings.mongodb_uri, tz_aware=True,
        serverSelectionTimeoutMS=config.mongo.server_selection_timeout_ms,
        connectTimeoutMS=config.mongo.connect_timeout_ms,
        socketTimeoutMS=config.mongo.socket_timeout_ms,
        waitQueueTimeoutMS=config.mongo.wait_queue_timeout_ms,
    )
    repository = MongoJobRepository(
        client[settings.database_name][config.mongo.jobs_collection],
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


def purge_core_scratch(root=None):
    """Remove temporary directories a killed worker left in its scratch mount.

    TemporaryDirectory cleanup does not run after OOM kills or SIGKILL, and the
    scratch path is a bind mount that survives container restarts.
    """
    import tempfile
    from imager_config.loader import config
    from imager_contracts.scratch import purge_stale_entries
    from src.shared_kernel.loggers import presentation_logger
    storage = config.storage
    prefixes = (storage.render_temp_prefix, storage.cell_temp_prefix, storage.manifest_temp_prefix,
                storage.json_temp_prefix, config.pack_import.temp_prefix)
    try:
        removed = purge_stale_entries(root or tempfile.gettempdir(), prefixes, storage.scratch_max_age_seconds)
    except OSError:
        presentation_logger.exception('Cannot sweep worker scratch directory')
        return 0
    if removed:
        presentation_logger.warning(f'Removed {removed} stale scratch entries left by a stopped worker')
    return removed
