import hashlib
import json
import os
import re
import tempfile
from dataclasses import asdict, is_dataclass
from pathlib import Path


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
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


class MinioObjectStore:
    """Immutable object references; temporary files are owned by the caller.

    A MinIO client belongs to one process. Encoded images are streamed with one
    upload worker instead of being retained as another full in-memory image.
    """

    def __init__(self, endpoint, access_key, secret_key, bucket, secure=False,
                 client=None):
        if not bucket or not isinstance(bucket, str):
            raise ValueError('MinIO bucket is required')
        self.bucket = bucket
        if client is None:
            from minio import Minio
            import urllib3
            client = Minio(endpoint, access_key=access_key, secret_key=secret_key,
                           secure=secure, http_client=urllib3.PoolManager(
                               timeout=urllib3.Timeout(connect=5, read=30),
                               retries=urllib3.Retry(total=2, backoff_factor=0.2),
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
        if (not isinstance(value, str) or not value or len(value) > 1024
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
        if not isinstance(digest, str) or not re.fullmatch(r'[a-f0-9]{64}', digest):
            raise ValueError('Object reference requires a lowercase SHA256 digest')
        size = ref.get('size_bytes')
        if type(size) is not int or not 0 < size <= max_bytes:
            raise ValueError(f'Object size must be between 1 and {max_bytes} bytes')
        return ref

    def upload_file(self, path, key, content_type='application/octet-stream'):
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
                metadata={'sha256': digest}, num_parallel_uploads=1,
            )
        except Exception as error:
            raise ObjectStoreError(error) from error
        return ref

    def download_file(self, reference, path, max_bytes=64_000_000):
        ref = self.validate_reference(reference, max_bytes)
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix='.download-', dir=target.parent)
        response = None
        try:
            response = self.client.get_object(ref['bucket'], ref['key'])
            digest, size = hashlib.sha256(), 0
            with os.fdopen(descriptor, 'wb') as stream:
                descriptor = None
                while True:
                    chunk = response.read(1024 * 1024)
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

    def read_json(self, reference, max_bytes=16_000_000):
        with tempfile.TemporaryDirectory(prefix='imager-manifest-') as directory:
            path = self.download_file(reference, Path(directory) / 'manifest.json', max_bytes)
            with open(path, encoding='utf-8') as stream:
                return json.load(stream)

    def put_json(self, value, key):
        encoded = json.dumps(value, sort_keys=True, separators=(',', ':'),
                             ensure_ascii=False).encode('utf-8')
        if len(encoded) > 16_000_000:
            raise ValueError('Catalog manifest exceeds 16 MB')
        with tempfile.TemporaryDirectory(prefix='imager-json-') as directory:
            path = Path(directory) / 'manifest.json'
            path.write_bytes(encoded)
            return self.upload_file(path, key, 'application/json')


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
