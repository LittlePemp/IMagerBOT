from datetime import datetime

from pymongo.asynchronous.collection import AsyncCollection

from settings import settings
from src.infrastructure.services.imager_service.schemas import GroupInfo
from src.models.image_build_params import validate_display_name
from src.models.image_group import GroupStatus, ImageGroup
from src.utils.building_blocks.result import Result
from src.utils.loggers import exception_logger


def revision_filter(revision):
    return {'$or': [{'revision': 0}, {'revision': {'$exists': False}}]} if revision == 0 else {'revision': revision}


class ImageGroupRepository:
    def __init__(self, collection: AsyncCollection):
        self.collection = collection

    async def initialize_indexes(self):
        await self.collection.create_index('imager_name', unique=True)

    @staticmethod
    def _read(row):
        row = dict(row)
        try:
            validate_display_name(row.get('display_name'))
        except ValueError:
            # Old unvalidated renames must remain repairable from the admin UI.
            exception_logger.warning(f'Invalid legacy group label: group={row["imager_name"]}')
            row['display_name'] = row['imager_name']
        return ImageGroup(**row)

    async def sync_groups(self, groups: list[GroupInfo]):
        now = datetime.now(settings.tzinfo)
        names = [group.group for group in groups]
        for group in groups:
            await self.collection.update_one(
                {'imager_name': group.group},
                {'$set': {'count': group.count, 'available_in_core': group.count > 0, 'last_synced': now},
                 '$setOnInsert': {'display_name': group.group, 'status': GroupStatus.ACTIVE, 'revision': 0}},
                upsert=True,
            )
        await self.collection.update_many(
            {'imager_name': {'$nin': names}},
            {'$set': {'available_in_core': False, 'count': 0, 'last_synced': now}},
        )

    async def get_groups(self, status: GroupStatus | None = GroupStatus.ACTIVE) -> Result:
        query = {} if status is None else {'status': status}
        if status == GroupStatus.ACTIVE:
            query.update({'available_in_core': {'$ne': False}, 'count': {'$gt': 0}})
        rows = await self.collection.find(query).sort('display_name', 1).to_list(length=None)
        return Result.Success([self._read(row) for row in rows])

    async def get_group_by_name(self, imager_name: str) -> ImageGroup | None:
        row = await self.collection.find_one({'imager_name': imager_name})
        return self._read(row) if row else None

    async def update_group_name(self, imager_name: str, new_display_name: str, *, expected_revision=None):
        return await self._update(imager_name, {'display_name': validate_display_name(new_display_name)}, expected_revision)

    async def update_group_status(self, imager_name: str, status: GroupStatus, *, expected_revision=None):
        return await self._update(imager_name, {'status': GroupStatus(status)}, expected_revision)

    async def _update(self, imager_name, changes, expected_revision):
        query = {'imager_name': imager_name}
        if expected_revision is not None:
            query.update(revision_filter(expected_revision))
        result = await self.collection.update_one(query, {'$set': changes, '$inc': {'revision': 1}})
        if not result.matched_count:
            raise ValueError('Библиотека уже изменена или удалена. Откройте список заново.')
        return await self.get_group_by_name(imager_name)
