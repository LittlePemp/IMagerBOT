import hashlib
import json
import os
import re
import tempfile
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock

from src.shared_kernel.resource_limits import configure_decoder_limits

configure_decoder_limits()

import cv2
import numpy as np
from scipy.spatial import KDTree
from src.domain.image_builder.services.image_service import ImageService
from src.shared_kernel.loggers import db_logger
from src.shared_kernel.resource_limits import (
    CACHE_LIMIT_BYTES, MEMORY_HEADROOM_BYTES, MEMORY_LIMIT_BYTES,
    current_rss_bytes, require_memory,
)
from src.shared_kernel.result import Result

from .minio_store import file_sha256, reference_dict


def validate_group_name(value):
    if (not isinstance(value, str) or not value.strip() or len(value) > 128
            or value in ('.', '..') or '/' in value or '\\' in value
            or '\x00' in value):
        raise ValueError('Catalog group must be a non-empty name without path separators')
    return value


@dataclass(frozen=True, slots=True)
class CatalogCell:
    object_ref: dict
    color: tuple
    relative_file_path: str

    @property
    def cell(self):
        # Keep the renderer's existing small catalog port stable.
        return self


class ManifestCatalog:
    """One immutable catalog revision, with a lazy bounded cache of originals."""

    def __init__(self, store, manifest_ref, group_name):
        require_memory(64_000_000)
        manifest = store.read_json(manifest_ref)
        if (not isinstance(manifest, dict) or manifest.get('schema_version') != 1
                or manifest.get('group_name') != group_name
                or manifest.get('normalization') != 'opencv-uint8-bgr-v1'):
            raise ValueError('Unsupported or mismatched catalog manifest')
        images = manifest.get('images')
        if not isinstance(images, list) or not 1 <= len(images) <= 100_000:
            raise ValueError('Catalog must contain between 1 and 100000 images')
        cells, identities = [], set()
        for index, image in enumerate(images):
            if index % 128 == 0:
                require_memory(32_000_000)
            if not isinstance(image, dict):
                raise ValueError('Invalid catalog entry')
            ref = store.validate_reference(image.get('object'), 64_000_000)
            color = image.get('color')
            if (not isinstance(color, list) or len(color) != 3
                    or any(type(value) is not int or not 0 <= value <= 255 for value in color)):
                raise ValueError('Catalog color must be three integer BGR channels')
            if not ref['key'].startswith('catalog/images/'):
                raise ValueError('Catalog image reference must use the catalog image prefix')
            identity = (ref['bucket'], ref['key'], ref['sha256'])
            if identity in identities:
                continue
            identities.add(identity)
            cells.append(CatalogCell(ref, tuple(color), f"s3://{ref['bucket']}/{ref['key']}"))
        self.store, self.group_name = store, group_name
        self.cells = cells
        self.tree = KDTree([cell.color for cell in cells])
        self._images = OrderedDict()
        self._cache_bytes = 0
        self._lock = RLock()

    @property
    def cache_bytes(self):
        return self._cache_bytes

    def trim_image_cache(self, target_bytes=0):
        with self._lock:
            while self._images and self._cache_bytes > max(0, target_bytes):
                _, removed = self._images.popitem(last=False)
                self._cache_bytes -= removed.nbytes

    def find_closest_metadata(self, colors, group):
        if group != self.group_name:
            return Result.Error(f'Catalog manifest does not describe group {group}')
        try:
            if len(colors) == 0:
                return Result.Success([])
            _, indices = self.tree.query(colors)
            return Result.Success([self.cells[int(index)] for index in np.atleast_1d(indices)])
        except Exception as error:
            db_logger.exception(f'Catalog query failed: {error}')
            return Result.Error(str(error))

    def get_cell_image(self, metadata):
        with self._lock:
            key = metadata.relative_file_path
            image = self._images.get(key)
            if image is not None:
                self._images.move_to_end(key)
                return Result.Success(image)
            try:
                while (self._images and current_rss_bytes() + 256_000_000
                       + MEMORY_HEADROOM_BYTES > MEMORY_LIMIT_BYTES):
                    self.trim_image_cache(max(0, self._cache_bytes - 32_000_000))
                require_memory(256_000_000)
                with tempfile.TemporaryDirectory(prefix='imager-cell-') as directory:
                    path = self.store.download_file(
                        metadata.object_ref, Path(directory) / 'original.bin',
                    )
                    decoded = cv2.imread(path, cv2.IMREAD_UNCHANGED)
                    validation = ImageService.validate_image(decoded)
                    if not validation:
                        return validation
                    normalized = ImageService.normalize_image(decoded)
                    if not normalized:
                        return normalized
                    image = normalized.value
                if image.nbytes <= CACHE_LIMIT_BYTES:
                    self.trim_image_cache(CACHE_LIMIT_BYTES - image.nbytes)
                    self._images[key] = image
                    self._cache_bytes += image.nbytes
                return Result.Success(image)
            except Exception as error:
                db_logger.exception(f'Cannot read catalog object {key}: {error}')
                return Result.Error(error)


class MinioCatalogProvider:
    """Worker-scoped provider; preserve the current revision's warmed originals."""

    def __init__(self, store):
        self.store = store
        self._catalog = None
        self._identity = None
        self._lock = RLock()

    def for_manifest(self, manifest_ref, group_name):
        ref = reference_dict(manifest_ref)
        identity = (group_name, ref['bucket'], ref['key'], ref['sha256'])
        with self._lock:
            if identity != self._identity:
                if self._catalog is not None:
                    self._catalog.trim_image_cache(0)
                self._catalog = None
                self._identity = None
                catalog = ManifestCatalog(self.store, ref, group_name)
                self._catalog, self._identity = catalog, identity
            return self._catalog


def import_local_catalog(directory, store, groups_collection, group_name=None):
    """Explicit one-off import. Local files are read, never moved or deleted."""
    base = Path(directory).resolve()
    if not base.is_dir():
        raise ValueError('Catalog import directory does not exist')
    if group_name is not None:
        groups = [(validate_group_name(group_name), base)]
    else:
        groups = [(validate_group_name(path.name), path)
                  for path in sorted(base.iterdir()) if path.is_dir()]
    store.ensure_bucket()
    report = []
    extensions = {'.png', '.jpg', '.jpeg', '.bmp', '.tif', '.tiff'}
    for name, group_path in groups:
        images, seen_hashes = [], set()
        for path in sorted(group_path.iterdir()):
            if not path.is_file() or path.suffix.lower() not in extensions:
                continue
            if path.stat().st_size > 64_000_000:
                raise ValueError(f'Catalog image exceeds 64 MB: {path.name}')
            require_memory(256_000_000)
            decoded = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
            validation = ImageService.validate_image(decoded)
            if not validation:
                raise ValueError(f'{path.name}: {validation.error}')
            normalized = ImageService.normalize_image(decoded)
            if not normalized:
                raise ValueError(f'{path.name}: {normalized.error}')
            digest = file_sha256(path)
            if digest in seen_hashes:
                continue
            seen_hashes.add(digest)
            color = ImageService.average_color(normalized.value)
            if not color:
                raise ValueError(str(color.error))
            extension = path.suffix.lower()
            key = f'catalog/images/{digest}{extension}'
            mime = 'image/jpeg' if extension in ('.jpg', '.jpeg') else (
                'image/png' if extension == '.png' else 'application/octet-stream'
            )
            reference = store.upload_file(path, key, mime)
            height, width = normalized.value.shape[:2]
            images.append({'object': reference, 'color': list(color.value),
                           'width': width, 'height': height})
            if len(images) > 100_000:
                raise ValueError('Catalog group exceeds 100000 images')
        if not images:
            raise ValueError(f'Catalog group {name} contains no supported images')
        manifest = dict(schema_version=1, group_name=name,
                        normalization='opencv-uint8-bgr-v1', images=images)
        encoded = json.dumps(manifest, sort_keys=True, separators=(',', ':'),
                             ensure_ascii=False).encode('utf-8')
        revision = hashlib.sha256(encoded).hexdigest()
        # The group is metadata; content-addressed objects avoid unsafe names.
        group_key = hashlib.sha256(name.encode('utf-8')).hexdigest()[:24]
        manifest_ref = store.put_json(manifest, f'catalog/manifests/{group_key}/{revision}.json')
        document = dict(group=name, name=name, count=len(images), revision=revision,
                        manifest=manifest_ref, updated_at=datetime.now(timezone.utc))
        groups_collection.update_one({'_id': name}, {'$set': document}, upsert=True)
        report.append(document)
    return report
