import hashlib
import json
import tempfile
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock

from imager_config.loader import config
from imager_contracts import ObjectRef
from imager_contracts.protocol import (
    BINARY_CONTENT_TYPE, CATALOG_NORMALIZATION, COLOR_CHANNEL_MAX, COLOR_CHANNELS,
    GROUP_DIGEST_LENGTH, GROUP_NAME_MAX_LENGTH, JPEG_CONTENT_TYPE, JPEG_EXTENSIONS,
    JSON_CONTENT_TYPE, PNG_CONTENT_TYPE, SCHEMA_VERSION,
)
from src.shared_kernel.resource_limits import configure_decoder_limits

configure_decoder_limits()

import cv2
import numpy as np
from scipy.spatial import KDTree
from src.domain.image_builder.services.image_service import ImageService
from src.shared_kernel.loggers import db_logger
from src.shared_kernel.resource_limits import current_rss_bytes, require_memory
from src.shared_kernel.result import Result

from .minio_store import file_sha256, reference_dict


def validate_group_name(value):
    if (not isinstance(value, str) or not value.strip() or len(value) > GROUP_NAME_MAX_LENGTH
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
        require_memory(config.memory.manifest_reserve_bytes)
        manifest = store.read_json(manifest_ref)
        if (not isinstance(manifest, dict) or manifest.get('schema_version') != SCHEMA_VERSION
                or manifest.get('group_name') != group_name
                or manifest.get('normalization') != CATALOG_NORMALIZATION):
            raise ValueError('Unsupported or mismatched catalog manifest')
        images = manifest.get('images')
        if not isinstance(images, list) or not 1 <= len(images) <= config.catalog.max_images:
            raise ValueError(f'Catalog must contain between 1 and {config.catalog.max_images} images')
        cells, identities = [], set()
        for index, image in enumerate(images):
            if index % config.catalog.validation_interval == 0:
                require_memory(config.memory.catalog_check_reserve_bytes)
            if not isinstance(image, dict):
                raise ValueError('Invalid catalog entry')
            ref = store.validate_reference(image.get('object'), config.limits.source_file_bytes)
            color = image.get('color')
            if (not isinstance(color, list) or len(color) != COLOR_CHANNELS
                    or any(type(value) is not int or not 0 <= value <= COLOR_CHANNEL_MAX
                           for value in color)):
                raise ValueError('Catalog color must be three integer BGR channels')
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
                while (self._images and current_rss_bytes() + config.memory.decode_reserve_bytes
                       + config.limits.memory_headroom_bytes > config.limits.memory_bytes):
                    self.trim_image_cache(max(
                        0, self._cache_bytes - config.memory.cache_eviction_step_bytes,
                    ))
                require_memory(config.memory.decode_reserve_bytes)
                with tempfile.TemporaryDirectory(prefix=config.storage.cell_temp_prefix) as directory:
                    path = self.store.download_file(
                        metadata.object_ref, Path(directory) / config.storage.cell_filename,
                    )
                    decoded = cv2.imread(path, cv2.IMREAD_UNCHANGED)
                    validation = ImageService.validate_image(decoded)
                    if not validation:
                        return validation
                    normalized = ImageService.normalize_image(decoded)
                    if not normalized:
                        return normalized
                    image = normalized.value
                if image.nbytes <= config.limits.original_cache_bytes:
                    self.trim_image_cache(config.limits.original_cache_bytes - image.nbytes)
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


def _local_groups(directory, group_name):
    base = Path(directory).resolve()
    if not base.is_dir():
        raise ValueError('Catalog import directory does not exist')
    if group_name is not None:
        groups = [(validate_group_name(group_name), base)]
    else:
        groups = [(validate_group_name(path.name), path)
                  for path in sorted(base.iterdir(), key=lambda item: item.name) if path.is_dir()]
    if not groups:
        raise ValueError('Catalog root contains no group directories; use --group-name for one image directory')
    return groups


def _local_image_info(path):
    """Return metadata only; release every decoded array before the next file.

    Keeping this frame separate also releases validation Result.value aliases
    when an unsupported dtype or another image validation fails.
    """
    require_memory(config.memory.decode_reserve_bytes)
    decoded = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
    validation = ImageService.validate_image(decoded)
    if not validation:
        raise ValueError(validation.error)
    normalized = ImageService.normalize_image(decoded)
    if not normalized:
        raise ValueError(normalized.error)
    color = ImageService.average_color(normalized.value)
    if not color:
        raise ValueError(color.error)
    height, width = normalized.value.shape[:2]
    return color.value, width, height, normalized.value.nbytes


def _local_group_plan(name, group_path, bucket, *, progress=None, skip_invalid=False):
    """Hash/validate a group with one decoded image resident at a time.

    ``skip_invalid`` (used for uploaded packs) skips and reports a broken or
    oversized image instead of rejecting the whole group.
    """
    images, sources, seen_hashes = [], [], set()
    invalid = {'count': 0, 'samples': []}

    def reject(path, reason):
        if not skip_invalid:
            raise ValueError(f'{path.name}: {reason}')
        invalid['count'] += 1
        if len(invalid['samples']) < 20:
            invalid['samples'].append(f'{path.name}: {reason}'[:200])
        if progress:
            progress({'phase': 'validating', 'processed': source_files, 'total': total, 'path': str(path)})
    source_files = source_bytes = image_bytes = cache_bytes = duplicate_files = 0
    widths, heights = [], []
    total = sum(1 for path in group_path.iterdir()
                if path.is_file() and path.suffix.lower() in config.catalog.allowed_extensions) if progress else 0
    for path in sorted(group_path.iterdir(), key=lambda item: item.name):
        if not path.is_file() or path.suffix.lower() not in config.catalog.allowed_extensions:
            continue
        size = path.stat().st_size
        source_files += 1
        source_bytes += size
        if not 0 < size <= config.limits.source_file_bytes:
            reject(path, f'encoded image size must fit {config.limits.source_file_bytes} bytes')
            continue
        digest = file_sha256(path)
        if digest in seen_hashes:
            duplicate_files += 1
            if progress:
                progress({'phase': 'validating', 'processed': source_files, 'total': total, 'path': str(path)})
            continue
        if len(images) >= config.catalog.max_images:
            raise ValueError(f'Catalog group exceeds {config.catalog.max_images} images')
        try:
            color, width, height, decoded_bytes = _local_image_info(path)
        except cv2.error as error:
            if not skip_invalid:
                raise
            reject(path, f'cannot decode image ({error.__class__.__name__})')
            continue
        except ValueError as error:
            reject(path, str(error))
            continue
        extension = path.suffix.lower()
        key = config.storage.catalog_image_key_template.format(sha256=digest, extension=extension)
        mime = JPEG_CONTENT_TYPE if extension in JPEG_EXTENSIONS else (
            PNG_CONTENT_TYPE if extension == '.png' else BINARY_CONTENT_TYPE
        )
        reference = ObjectRef(bucket, key, digest, size, mime).to_dict()
        images.append({'object': reference, 'color': list(color),
                       'width': width, 'height': height})
        sources.append((path, reference))
        widths.append(width)
        heights.append(height)
        image_bytes += size
        cache_bytes += decoded_bytes
        seen_hashes.add(digest)
        if progress:
            progress({'phase': 'validating', 'processed': source_files, 'total': total, 'path': str(path)})
    if not images:
        raise ValueError(f'Catalog group {name} contains no supported images')
    manifest = dict(schema_version=SCHEMA_VERSION, group_name=name,
                    normalization=CATALOG_NORMALIZATION, images=images)
    encoded = json.dumps(manifest, sort_keys=True, separators=(',', ':'),
                         ensure_ascii=False).encode('utf-8')
    if len(encoded) > config.catalog.max_manifest_bytes:
        raise ValueError(f'Catalog manifest exceeds {config.catalog.max_manifest_bytes} bytes')
    revision = hashlib.sha256(encoded).hexdigest()
    group_id = hashlib.sha256(name.encode('utf-8')).hexdigest()[:GROUP_DIGEST_LENGTH]
    manifest_key = config.storage.catalog_manifest_key_template.format(group_id=group_id, sha256=revision)
    manifest_ref = ObjectRef(bucket, manifest_key, revision, len(encoded), JSON_CONTENT_TYPE).to_dict()
    report = dict(
        group=name, name=name, count=len(images), revision=revision, manifest=manifest_ref,
        source_files=source_files, duplicate_files=duplicate_files,
        source_bytes=source_bytes, unique_image_bytes=image_bytes, manifest_bytes=len(encoded),
        additional_payload_bytes_upper_bound=image_bytes + len(encoded),
        normalized_cache_bytes=cache_bytes,
        width_min=min(widths), width_max=max(widths), height_min=min(heights), height_max=max(heights),
        invalid_files=invalid['count'], invalid_samples=invalid['samples'],
    )
    return report, manifest, sources


def preview_local_catalog(directory, group_name=None, bucket=None):
    """Offline preflight: read originals; never create buckets or update Mongo."""
    bucket = config.storage.bucket if bucket is None else bucket
    return [_local_group_plan(name, path, bucket)[0]
            for name, path in _local_groups(directory, group_name)]


def import_local_catalog(directory, store, groups_collection, group_name=None):
    """Publish validated immutable objects, then atomically replace each group.

    Repeated imports reuse object keys and manifest revisions. Originals are
    read only, and objects absent from a newer revision are never deleted here.
    Capacity reports count payload bytes before reuse; MinIO filesystem metadata
    and unrelated existing objects are outside this conservative estimate.
    """
    report = []
    for name, group_path in _local_groups(directory, group_name):
        document, manifest, sources = _local_group_plan(name, group_path, store.bucket)
        store.ensure_bucket()
        for path, expected in sources:
            reference = store.upload_file(path, expected['key'], expected['content_type'])
            if reference != expected:
                raise ValueError(f'{path.name}: source changed while importing; catalog was not published')
        manifest_ref = store.put_json(manifest, document['manifest']['key'])
        if manifest_ref != document['manifest']:
            raise ValueError('Published manifest does not match the validated import plan')
        published = dict(
            group=name, name=name, count=document['count'], revision=document['revision'],
            manifest=manifest_ref, updated_at=datetime.now(timezone.utc),
        )
        groups_collection.update_one({'_id': name}, {'$set': published}, upsert=True)
        report.append({**document, 'updated_at': published['updated_at']})
    return report
