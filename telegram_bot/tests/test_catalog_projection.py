import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from src.application.catalog_projection import CatalogImportProjection


class CatalogProjectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_completed_import_becomes_available_without_admin_polling(self):
        imports = SimpleNamespace(
            unsynced_completed=AsyncMock(return_value=['import-1']), mark_catalog_synced=AsyncMock(),
            core_gateway=SimpleNamespace(list_groups=AsyncMock(return_value=SimpleNamespace(groups=['all-groups']))),
        )
        groups = SimpleNamespace(sync_groups=AsyncMock())
        self.assertTrue(await CatalogImportProjection(imports, groups).sync_once())
        groups.sync_groups.assert_awaited_once_with(['all-groups'])
        imports.mark_catalog_synced.assert_awaited_once_with(['import-1'])

    async def test_failed_projection_remains_retryable(self):
        imports = SimpleNamespace(
            unsynced_completed=AsyncMock(return_value=['import-1']), mark_catalog_synced=AsyncMock(),
            core_gateway=SimpleNamespace(list_groups=AsyncMock(return_value=SimpleNamespace(groups=[]))),
        )
        groups = SimpleNamespace(sync_groups=AsyncMock(side_effect=OSError('database down')))
        with self.assertRaises(OSError):
            await CatalogImportProjection(imports, groups).sync_once()
        imports.mark_catalog_synced.assert_not_awaited()
