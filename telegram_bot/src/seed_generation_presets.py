"""Explicit, idempotent preset initialization; does not start the bot."""
import asyncio


async def main():
    from pymongo import AsyncMongoClient

    from imager_config.loader import config
    from settings import settings
    from src.application.generation_presets import seed_generation_presets
    from src.infrastructure.data.unit_of_work import get_uow

    client = AsyncMongoClient(
        settings.mongodb_uri, tz_aware=True,
        serverSelectionTimeoutMS=config.mongo.server_selection_timeout_ms,
        connectTimeoutMS=config.mongo.connect_timeout_ms,
        socketTimeoutMS=config.mongo.socket_timeout_ms,
        waitQueueTimeoutMS=config.mongo.wait_queue_timeout_ms,
    )
    try:
        repository = get_uow(client[settings.database_name]).image_build_params_repository
        await repository.initialize_indexes()
        count = await seed_generation_presets(repository.collection)
        print(f'Generation profiles initialized: {count}.')
    finally:
        await client.close()


if __name__ == '__main__':
    asyncio.run(main())
