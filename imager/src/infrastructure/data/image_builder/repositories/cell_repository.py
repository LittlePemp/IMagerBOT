import os
from collections import OrderedDict, defaultdict
from threading import RLock
from imager_config.loader import config as runtime_config

import numpy as np
from pymongo.collection import Collection
from src.domain.image_builder.aggregates.cell import Cell
from src.domain.image_builder.entities.cell_object import CellObject
from src.domain.image_builder.repository_interface import IRepository
from src.domain.image_builder.services.kdtree_service import KDTreeService
from src.domain.image_builder.value_objects.cell_rgb import CellRgb
from src.shared_kernel.loggers import db_logger
from src.shared_kernel.resource_limits import (
    CACHE_LIMIT_BYTES, MEMORY_HEADROOM_BYTES, MEMORY_LIMIT_BYTES, current_rss_bytes, generation_slot,
    require_memory,
)
from src.shared_kernel.result import Result

from .file_repository import FileRepository


class CellRepository(IRepository):
    """Shared metadata/index and a bounded cache of decoded original images."""

    _instance = None
    _instance_lock = RLock()

    def __new__(cls, *args, **kwargs):
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self, mongo_collection: Collection, file_repository: FileRepository):
        with self._instance_lock:
            if not hasattr(self, '_initialized'):
                self.mongo_collection = mongo_collection
                self.file_repository = file_repository
                self._data = defaultdict(dict)
                self._trees = {}
                self._image_cache = OrderedDict()
                self._cache_bytes = 0
                self._lock = RLock()
                self._initialized = False

    def initialize(self):
        with generation_slot(), self._lock:
            if not self._initialized:
                self.refresh()
                self._initialized = True
                db_logger.info('CellRepository initialized')

    def refresh(self):
        with generation_slot(), self._lock:
            self.trim_image_cache(0)
            self._data = defaultdict(dict)
            self._trees = {}
            self.load_data_from_mongo()
            self.__build_trees()
            self.load_images()

    def load_data_from_mongo(self):
        """Load metadata only: decoding here used to load every original twice."""
        for index, config in enumerate(self.mongo_collection.find({})):
            if index % runtime_config.catalog.validation_interval == 0:
                require_memory(runtime_config.memory.catalog_check_reserve_bytes)
            try:
                path = self.file_repository.resolve_image_path(config['relative_file_path'])
                if not os.path.isfile(path):
                    db_logger.warning(f'Skipping missing catalog image: {path}')
                    continue
                rgb = CellRgb.create(config['r'], config['g'], config['b'])
                if not rgb.is_success:
                    db_logger.error(f'Invalid catalog color at {path}: {rgb.error}')
                    continue
                cell = Cell.create(rgb.value, config['group'], path)
                if not cell.is_success:
                    db_logger.error(f'Invalid catalog image {path}: {cell.error}')
                    continue
                metadata = CellObject.create(cell.value, load_image=False)
                self._data[config['group']][path] = metadata.value
            except (KeyError, TypeError, ValueError) as exc:
                db_logger.error(f'Skipping malformed catalog record: {exc}')

    def load_images(self):
        """Warm originals while they fit; larger libraries use the same LRU lazily."""
        with self._lock:
            previous_size = 0
            for cells in self._data.values():
                for metadata in cells.values():
                    if self._cache_bytes + previous_size > CACHE_LIMIT_BYTES:
                        return
                    if current_rss_bytes() + runtime_config.memory.decode_reserve_bytes + MEMORY_HEADROOM_BYTES > MEMORY_LIMIT_BYTES:
                        return
                    result = self.get_cell_image(metadata)
                    if not result.is_success:
                        db_logger.warning(f'Cannot warm original image: {result.error}')
                        continue
                    previous_size = result.value.nbytes
                    if self._cache_bytes >= CACHE_LIMIT_BYTES:
                        return

    def trim_image_cache(self, target_bytes: int = 0):
        with self._lock:
            while self._image_cache and self._cache_bytes > max(0, target_bytes):
                _, image = self._image_cache.popitem(last=False)
                self._cache_bytes -= image.nbytes

    @property
    def cache_bytes(self):
        return self._cache_bytes

    def get_cell_image(self, metadata: CellObject) -> Result:
        with self._lock:
            path = metadata.cell.relative_file_path
            image = self._image_cache.get(path)
            if image is not None:
                self._image_cache.move_to_end(path)
                return Result.Success(image)
            # Decode plus uint16/alpha normalization can temporarily hold several arrays.
            decode_reserve = runtime_config.memory.decode_reserve_bytes
            while (self._image_cache and
                   current_rss_bytes() + decode_reserve + MEMORY_HEADROOM_BYTES > MEMORY_LIMIT_BYTES):
                self.trim_image_cache(max(0, self._cache_bytes - runtime_config.memory.cache_eviction_step_bytes))
            if current_rss_bytes() + decode_reserve + MEMORY_HEADROOM_BYTES > MEMORY_LIMIT_BYTES:
                message = 'Not enough memory to decode a catalog image within the process budget'
                db_logger.error(message)
                return Result.Error(message)
            result = self.file_repository.read_image_file(path)
            if not result.is_success:
                return result
            normalized = self.file_repository.image_service.normalize_image(result.value)
            if not normalized.is_success:
                db_logger.error(f'Cannot normalize catalog image {path}: {normalized.error}')
                return normalized
            image = normalized.value
            if image.nbytes <= CACHE_LIMIT_BYTES:
                self.trim_image_cache(CACHE_LIMIT_BYTES - image.nbytes)
                if current_rss_bytes() + MEMORY_HEADROOM_BYTES < MEMORY_LIMIT_BYTES:
                    self._image_cache[path] = image
                    self._cache_bytes += image.nbytes
            return Result.Success(image)

    def find_closest_metadata(self, targets, group_name: str) -> Result:
        with self._lock:
            tree, cells = self._trees.get(group_name, (None, None))
            if tree is None or not cells:
                return Result.Error(f'Image group is missing or empty: {group_name}')
            if len(targets) == 0:
                return Result.Success([])
            try:
                _, indices = tree.query(targets)
                return Result.Success([cells[int(index)] for index in np.atleast_1d(indices)])
            except Exception as exc:
                db_logger.error(f'Cannot search catalog group {group_name}: {exc}')
                return Result.Error(f'Cannot search catalog group {group_name}: {exc}')

    def find_closest_cell(self, pixel_rgb: tuple[int, int, int], group_name: str) -> CellObject:
        result = self.find_closest_metadata([pixel_rgb], group_name)
        if not result.is_success:
            db_logger.error(str(result.error))
            return None
        metadata = result.value[0]
        image = self.get_cell_image(metadata)
        if not image.is_success:
            return None
        return CellObject.create(metadata.cell, image=image.value).value

    @staticmethod
    def _path_key(path):
        return os.path.normcase(os.path.realpath(path))

    def load_missing_groups(self, group_names: list[str] | None = None) -> Result:
        """Rare maintenance: reconcile additions, replacements, duplicates and deleted files."""
        with generation_slot(), self._lock:
            errors = []
            created_count = updated_count = removed_count = 0
            try:
                base_path = self.file_repository.settings.image_groups_relative_path
                all_groups = self._resolve_target_groups(base_path, group_names)
                requested = set(all_groups) if group_names is not None else None
                documents = list(self.mongo_collection.find({}))
                existing = {}
                for document in documents:
                    if requested is not None and document.get('group') not in requested:
                        continue
                    try:
                        key = self._path_key(self.file_repository.resolve_image_path(document['relative_file_path']))
                        if key in existing:
                            self.mongo_collection.delete_one({'_id': document['_id']})
                            removed_count += 1
                        else:
                            existing[key] = document
                    except (KeyError, TypeError, ValueError) as exc:
                        db_logger.error(f'Removing malformed catalog record: {exc}')
                        if '_id' in document:
                            self.mongo_collection.delete_one({'_id': document['_id']})
                            removed_count += 1

                seen, scanned_groups = set(), set()
                for group_name in all_groups:
                    group_path = os.path.join(base_path, group_name)
                    if not os.path.isdir(group_path):
                        scanned_groups.add(group_name)
                        continue
                    files = self.file_repository.list_image_files(group_path)
                    if not files.is_success:
                        errors.append(str(files.error))
                        continue
                    scanned_groups.add(group_name)
                    for image_file in files.value:
                        key = self._path_key(image_file)
                        seen.add(key)
                        previous = existing.get(key)
                        stat = os.stat(image_file)
                        if (previous is not None and previous.get('size_bytes') == stat.st_size
                                and previous.get('mtime_ns') == stat.st_mtime_ns
                                and previous.get('relative_file_path') == image_file):
                            continue
                        result = self._add_image_to_collection(group_name, image_file, previous)
                        if result.is_success:
                            if previous is None:
                                created_count += 1
                            else:
                                updated_count += 1
                        else:
                            errors.append(f'{image_file}: {result.error}')
                            if previous is not None:
                                self.mongo_collection.delete_one({'_id': previous['_id']})
                                removed_count += 1

                for key, document in existing.items():
                    scoped = requested is None or document.get('group') in scanned_groups
                    if scoped and key not in seen:
                        # A failed directory listing is not proof that its files were deleted.
                        if document.get('group') in all_groups and document.get('group') not in scanned_groups:
                            continue
                        self.mongo_collection.delete_one({'_id': document['_id']})
                        removed_count += 1
                self.mongo_collection.create_index('relative_file_path', unique=True)
            except Exception as exc:
                errors.append(str(exc))
            finally:
                # Rebuild even after partial failure or zero inserts: prior writes may already exist.
                try:
                    self.refresh()
                except Exception as exc:
                    errors.append(f'Cannot refresh catalog: {exc}')
            summary = f'Synchronized {created_count} images; updated {updated_count}; removed {removed_count}'
            if errors:
                message = summary + '; errors: ' + '; '.join(errors)
                db_logger.error(message)
                return Result.Error(message)
            db_logger.info(summary)
            return Result.Success(summary)

    def _resolve_target_groups(self, base_path: str, group_names: list[str] | None) -> list[str]:
        names = group_names if group_names is not None else [
            name for name in os.listdir(base_path) if os.path.isdir(os.path.join(base_path, name))
        ]
        for name in names:
            if (not isinstance(name, str) or not name.strip() or name in ('.', '..')
                    or '/' in name or chr(92) in name or os.path.isabs(name)):
                raise ValueError(f'Invalid image group name: {name}')
        return list(dict.fromkeys(names))

    def _get_existing_file_paths(self) -> set[str]:
        return {item['relative_file_path'] for item in self.mongo_collection.find({}, {'relative_file_path': 1})}

    def _add_image_to_collection(self, group_name: str, image_file: str, previous=None) -> Result:
        image_result = self.file_repository.read_image_file(image_file)
        if not image_result.is_success:
            return image_result
        normalized = self.file_repository.image_service.normalize_image(image_result.value)
        if not normalized.is_success:
            return normalized
        avg_color = self.file_repository.image_service.average_color(normalized.value)
        if not avg_color.is_success:
            return avg_color
        rgb = CellRgb.create(*avg_color.value[:3])
        if not rgb.is_success:
            return rgb
        cell = Cell.create(rgb.value, group_name, image_file)
        if not cell.is_success:
            return cell
        stat = os.stat(image_file)
        config = {
            'r': rgb.value.r.value, 'g': rgb.value.g.value, 'b': rgb.value.b.value,
            'group': group_name, 'relative_file_path': image_file,
            'size_bytes': stat.st_size, 'mtime_ns': stat.st_mtime_ns,
        }
        query = {'_id': previous['_id']} if previous is not None else {'relative_file_path': image_file}
        self.mongo_collection.update_one(query, {'$set': config}, upsert=True)
        return Result.Success(config)

    def get_all_groups(self) -> list[str]:
        return [result['_id'] for result in self.mongo_collection.aggregate([{'$group': {'_id': '$group'}}])]

    def get_all_groups_info(self) -> list[dict]:
        with self._lock:
            return [{'group': name, 'count': len(cells)} for name, cells in self._data.items() if cells]

    def __build_trees(self):
        result = KDTreeService.build_trees(self._data)
        if not result.is_success:
            raise RuntimeError(str(result.error))
        self._trees = result.value

    @property
    def data(self):
        return self._data
