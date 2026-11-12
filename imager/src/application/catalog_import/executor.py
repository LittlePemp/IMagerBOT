"""Bounded ZIP-to-immutable-catalog import, without Telegram dependencies."""
import shutil
import tempfile
from pathlib import Path

from imager_config.loader import config
from imager_contracts.catalog_import import CatalogImportRequest
from src.infrastructure.catalog_import.scratch import release_path_cache
from .archive import extract_catalog_archive
from .errors import InvalidCatalogArchive


class CatalogImportExecutor:
    def __init__(self, store, repository):
        self.store, self.repository = store, repository

    def execute(self, request, token, progress):
        from cv2 import error as OpenCVError
        from src.infrastructure.object_storage.catalog import _local_group_plan

        request = CatalogImportRequest.from_dict(request)
        self.repository.reserve_catalog(request.import_id, token, request.to_dict())
        policy = config.pack_import
        self.store.validate_reference(request.source.to_dict(), policy.max_archive_bytes)
        with tempfile.TemporaryDirectory(prefix=policy.temp_prefix) as directory:
            root = Path(directory)
            required = request.source.size_bytes + policy.max_expanded_bytes + policy.disk_headroom_bytes
            if shutil.disk_usage(root).free < required:
                raise InvalidCatalogArchive('Недостаточно свободного места для безопасной распаковки ZIP.')
            progress({'phase': 'downloading', 'processed': 0, 'total': 0})
            archive = root / policy.archive_filename
            self.store.download_file(request.source.to_dict(), archive, max_bytes=policy.max_archive_bytes)
            release_path_cache(archive, flush=True)
            images = root / policy.extraction_directory
            extracted = extract_catalog_archive(archive, images, policy=policy, progress=progress)
            release_path_cache(archive)
            progress({'phase': 'validating', 'processed': 0, 'total': extracted['files']})

            def validated(details):
                release_path_cache(details['path'])
                progress({name: details[name] for name in ('phase', 'processed', 'total')})

            try:
                report, manifest, sources = _local_group_plan(
                    request.group_name, images, self.store.bucket, progress=validated,
                )
            except (ValueError, OpenCVError) as error:
                raise InvalidCatalogArchive(f'Некорректное изображение в ZIP: {error}') from error
            self.store.ensure_bucket()
            for index, (path, expected) in enumerate(sources):
                progress({'phase': 'uploading', 'processed': index, 'total': len(sources)})
                reference = self.store.upload_file(path, expected['key'], expected['content_type'])
                release_path_cache(path)
                if reference != expected:
                    raise InvalidCatalogArchive('Содержимое изображения изменилось во время импорта.')
            reference = self.store.put_json(manifest, report['manifest']['key'])
            if reference != report['manifest']:
                raise InvalidCatalogArchive('Не удалось подтвердить контрольную сумму каталога.')
            progress({'phase': 'publishing', 'processed': len(sources), 'total': len(sources)})
            result = {
                'group_name': request.group_name, 'display_name': request.display_name,
                'count': report['count'], 'revision': report['revision'], 'manifest': reference,
            }
            return self.repository.publish_catalog(request.import_id, token, result)
