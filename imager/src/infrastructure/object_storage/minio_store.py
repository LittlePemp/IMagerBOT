import hashlib
import json
import os
import re
import tempfile
from dataclasses import asdict, is_dataclass
from pathlib import Path

from imager_config.loader import config
from imager_contracts.protocol import (
    BINARY_CONTENT_TYPE, JSON_CONTENT_TYPE, OBJECT_KEY_MAX_LENGTH, SHA256_PATTERN,
)


class ObjectIntegrityError(ValueError):
    """The object reference and the stored bytes do not describe the same asset."""

    retryable = False


class ObjectStoreError(RuntimeError):
    def __init__(self, error):
        super().__init__(str(error))
        self.code = getattr(error, 'code', 'object_store_unavailable')
        self.retryable = (
            self.code in ('InternalError', 'SlowDown', 'ServiceUnavailable', 'RequestTimeout')
            or isinstance(error, (ConnectionError, TimeoutError, OSError))
            or type(error).__module__.startswith('urllib3')
        )


def reference_dict(reference):
    if is_dataclass(reference):
        return asdict(reference)
    if not isinstance(reference, dict):
        raise ValueError('Object reference must be an object')
    return dict(reference)


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for chunk in iter(lambda: stream.read(config.storage.transfer_chunk_bytes), b''):
            digest.update(chunk)
    return digest.hexdigest()


class MinioObjectStore:
    """Immutable object references; temporary files are owned by the caller.

    A MinIO client belongs to one process. Encoded images are streamed with one
    upload worker instead of being retained as another full in-memory image.
    """

    def __init__(self, endpoint, access_key, secret_key, bucket, secure=None,
                 client=None):
        if not bucket or not isinstance(bucket, str):
            raise ValueError('MinIO bucket is required')
        self.bucket = bucket
        secure = config.storage.secure if secure is None else secure
        if client is None:
            from minio import Minio
            import urllib3
            client = Minio(endpoint, access_key=access_key, secret_key=secret_key,
                           secure=secure, http_client=urllib3.PoolManager(
                               timeout=urllib3.Timeout(
                                   connect=config.storage.connect_timeout_seconds,
                                   read=config.storage.read_timeout_seconds,
                               ),
                               retries=urllib3.Retry(
                                   total=config.storage.retry_attempts,
                                   backoff_factor=config.storage.retry_backoff_seconds,
                               ),
                           ))
        self.client = client

    def ensure_bucket(self):
        if not self.client.bucket_exists(self.bucket):
            try:
                self.client.make_bucket(self.bucket)
            except Exception as error:
                if getattr(error, 'code', None) not in (
                    'BucketAlreadyOwnedByYou', 'BucketAlreadyExists',
                ):
                    raise ObjectStoreError(error) from error

    @staticmethod
    def _key(value):
        if (not isinstance(value, str) or not value or len(value) > OBJECT_KEY_MAX_LENGTH
                or value.startswith('/') or '\\' in value or '\x00' in value
                or any(part in ('', '.', '..') for part in value.split('/'))):
            raise ValueError('Object key must be a normalized relative S3 key')
        return value

    def validate_reference(self, reference, max_bytes):
        ref = reference_dict(reference)
        if ref.get('bucket') != self.bucket:
            raise ValueError('Object reference uses a different bucket')
        self._key(ref.get('key'))
        digest = ref.get('sha256')
        if not isinstance(digest, str) or not re.fullmatch(SHA256_PATTERN, digest):
            raise ValueError('Object reference requires a lowercase SHA256 digest')
        size = ref.get('size_bytes')
        if type(size) is not int or not 0 < size <= max_bytes:
            raise ValueError(f'Object size must be between 1 and {max_bytes} bytes')
        return ref

    def upload_file(self, path, key, content_type=None):
        content_type = BINARY_CONTENT_TYPE if content_type is None else content_type
        key = self._key(key)
        size = os.path.getsize(path)
        if size <= 0:
            raise ValueError('Cannot upload an empty object')
        digest = file_sha256(path)
        ref = dict(bucket=self.bucket, key=key, sha256=digest,
                   size_bytes=size, content_type=content_type)
        try:
            existing = self.client.stat_object(self.bucket, key)
        except Exception as error:
            if getattr(error, 'code', None) not in ('NoSuchKey', 'NoSuchObject', 'NotFound'):
                raise ObjectStoreError(error) from error
        else:
            metadata = {str(k).lower(): str(v) for k, v in existing.metadata.items()}
            if (existing.size != size
                    or metadata.get('x-amz-meta-sha256') != digest):
                raise ObjectIntegrityError('Immutable object key already contains different bytes')
            return ref
        try:
            self.client.fput_object(
                self.bucket, key, str(path), content_type=content_type,
                metadata={'sha256': digest},
                num_parallel_uploads=config.storage.upload_parallelism,
            )
        except Exception as error:
            raise ObjectStoreError(error) from error
        return ref

    def download_file(self, reference, path, max_bytes=None):
        max_bytes = config.limits.source_file_bytes if max_bytes is None else max_bytes
        ref = self.validate_reference(reference, max_bytes)
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            prefix=config.storage.download_temp_prefix, dir=target.parent,
        )
        response = None
        try:
            response = self.client.get_object(ref['bucket'], ref['key'])
            digest, size = hashlib.sha256(), 0
            with os.fdopen(descriptor, 'wb') as stream:
                descriptor = None
                while True:
                    chunk = response.read(config.storage.transfer_chunk_bytes)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > ref['size_bytes'] or size > max_bytes:
                        raise ObjectIntegrityError('Object exceeded its declared size')
                    digest.update(chunk)
                    stream.write(chunk)
            if size != ref['size_bytes'] or digest.hexdigest() != ref['sha256']:
                raise ObjectIntegrityError('Object size or SHA256 verification failed')
            os.replace(temporary, target)
            temporary = None
            return str(target)
        except ObjectIntegrityError:
            raise
        except Exception as error:
            raise ObjectStoreError(error) from error
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if response is not None:
                try:
                    response.close()
                finally:
                    response.release_conn()
            if temporary is not None:
                try:
                    os.remove(temporary)
                except FileNotFoundError:
                    pass

    def read_json(self, reference, max_bytes=None):
        max_bytes = config.catalog.max_manifest_bytes if max_bytes is None else max_bytes
        with tempfile.TemporaryDirectory(prefix=config.storage.manifest_temp_prefix) as directory:
            path = self.download_file(
                reference, Path(directory) / config.storage.manifest_filename, max_bytes,
            )
            with open(path, encoding='utf-8') as stream:
                return json.load(stream)

    def put_json(self, value, key):
        encoded = json.dumps(value, sort_keys=True, separators=(',', ':'),
                             ensure_ascii=False).encode('utf-8')
        if len(encoded) > config.catalog.max_manifest_bytes:
            raise ValueError(f'Catalog manifest exceeds {config.catalog.max_manifest_bytes} bytes')
        with tempfile.TemporaryDirectory(prefix=config.storage.json_temp_prefix) as directory:
            path = Path(directory) / config.storage.manifest_filename
            path.write_bytes(encoded)
            return self.upload_file(path, key, JSON_CONTENT_TYPE)


def object_store_from_settings(settings):
    def secret(value):
        return value.get_secret_value() if hasattr(value, 'get_secret_value') else value

    return MinioObjectStore(
        endpoint=settings.minio_endpoint,
        access_key=secret(settings.minio_access_key),
        secret_key=secret(settings.minio_secret_key),
        bucket=settings.minio_bucket,
        secure=settings.minio_secure,
    )
