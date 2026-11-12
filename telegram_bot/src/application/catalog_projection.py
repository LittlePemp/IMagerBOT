"""Repair the bot catalog projection independently of import UI activity."""
import asyncio

from imager_config.loader import config
from src.utils.loggers import exception_logger


class CatalogImportProjection:
    def __init__(self, imports, groups):
        self.imports, self.groups = imports, groups

    async def sync_once(self):
        identities = await self.imports.unsynced_completed(config.pack_import.recent_limit)
        if not identities:
            return False
        snapshot = await self.imports.core_gateway.list_groups()
        await self.groups.sync_groups(snapshot.groups)
        await self.imports.mark_catalog_synced(identities)
        return True

    async def run(self, stop):
        while not stop.is_set():
            try:
                if await self.sync_once():
                    continue
            except asyncio.CancelledError:
                raise
            except Exception:
                exception_logger.exception('Cannot project imported catalogs into bot libraries')
            try:
                await asyncio.wait_for(stop.wait(), config.pack_import.catalog_sync_seconds)
            except asyncio.TimeoutError:
                pass
