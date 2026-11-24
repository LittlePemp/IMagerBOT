"""Telegram's S3 boundary. Files on disk are temporary transfer buffers only."""
import hashlib
import os
import re
import tempfile
from pathlib import Path

from imager_config.loader import config
from imager_contracts import ObjectRef
from imager_contracts.protocol import (
    BINARY_CONTENT_TYPE, JPEG_CONTENT_TYPE, PNG_CONTENT_TYPE, WEBP_CONTENT_TYPE, JOB_ID_PATTERN,
)


class BotObjectStorage:
    def __init__(self, endpoint, access_key, secret_key, bucket, *, secure=False, client=None):
        if not bucket:
            raise ValueError('MINIO_BUCKET is required')
        if client is None:
            if not endpoint or not access_key or not secret_key:
                raise ValueError('MinIO endpoint and credentials are required')
            from minio import Minio
            import urllib3
            client = Minio(
                endpoint, access_key=access_key, secret_key=secret_key, secure=secure,
                http_client=urllib3.PoolManager(
                    timeout=urllib3.Timeout(
                        connect=config.storage.connect_timeout_seconds,
                        read=config.storage.read_timeout_seconds,
                    ),
                    retries=urllib3.Retry(
                        total=config.storage.retry_attempts,
                        backoff_factor=config.storage.retry_backoff_seconds,
                    ),
                ),
            )
        self.client = client
        self.bucket = bucket

    @staticmethod
    def sha256_file(path):
        digest = hashlib.sha256()
        with open(path, 'rb') as stream:
            for chunk in iter(lambda: stream.read(config.storage.transfer_chunk_bytes), b''):
                digest.update(chunk)
        return digest.hexdigest()

    def upload_source(self, path, job_id, *, max_bytes=None, content_type=JPEG_CONTENT_TYPE):
        max_bytes = config.limits.source_file_bytes if max_bytes is None else max_bytes
        if content_type not in (JPEG_CONTENT_TYPE, PNG_CONTENT_TYPE, WEBP_CONTENT_TYPE, BINARY_CONTENT_TYPE):
            raise ValueError('Unsupported source image content type')
        size = os.path.getsize(path)
        if not 0 < size <= max_bytes:
            raise ValueError(f'Source file must contain between 1 and {max_bytes} bytes')
        if not isinstance(job_id, str) or not re.fullmatch(JOB_ID_PATTERN, job_id):
            raise ValueError('Invalid job identifier')
        checksum = self.sha256_file(path)
        key = config.storage.source_key_template.format(job_id=job_id, sha256=checksum)
        if not self.client.bucket_exists(self.bucket):
            try:
                self.client.make_bucket(self.bucket)
            except Exception as error:
                if getattr(error, 'code', '') not in ('BucketAlreadyExists', 'BucketAlreadyOwnedByYou'):
                    raise
        self.client.fput_object(
            self.bucket, key, str(path), content_type=content_type,
            metadata={'sha256': checksum}, num_parallel_uploads=config.storage.upload_parallelism,
        )
        return {
            'bucket': self.bucket, 'key': key, 'sha256': checksum,
            'size_bytes': size, 'content_type': content_type,
        }

    @staticmethod
    def source_prefix(job_id=None):
        """Key prefix owned by uploaded sources (optionally of one job)."""
        template = config.storage.source_key_template
        if job_id is None:
            return template.split('{', 1)[0]
        if not isinstance(job_id, str) or not re.fullmatch(JOB_ID_PATTERN, job_id):
            raise ValueError('Invalid job identifier')
        return template.split('{sha256}', 1)[0].format(job_id=job_id)

    @classmethod
    def source_job_id(cls, key):
        """Job identity encoded in an uploaded-source key, or None if foreign."""
        prefix = cls.source_prefix()
        if not isinstance(key, str) or not prefix or not key.startswith(prefix):
            return None
        job_id = key[len(prefix):].split('/', 1)[0]
        if not re.fullmatch(JOB_ID_PATTERN, job_id) or not key.startswith(cls.source_prefix(job_id)):
            return None
        return job_id

    def list_sources(self):
        """Yield (key, last_modified) for every uploaded-source object."""
        prefix = self.source_prefix()
        if not prefix:
            raise ValueError('Uploaded sources need a dedicated key prefix')
        for item in self.client.list_objects(self.bucket, prefix=prefix, recursive=True):
            if not item.is_dir:
                yield item.object_name, item.last_modified

    def delete_source(self, job_id, key):
        """Remove one uploaded source; only keys owned by ``job_id`` are accepted."""
        if not isinstance(key, str) or self.source_job_id(key) != job_id:
            raise ValueError('Object is not an uploaded source of this job')
        try:
            self.client.remove_object(self.bucket, key)
        except Exception as error:
            if getattr(error, 'code', None) not in ('NoSuchKey', 'NoSuchObject', 'NotFound'):
                raise

    def download_result(self, reference, path, *, max_bytes):
        reference = ObjectRef.from_dict(reference).to_dict()
        if reference.get('bucket') != self.bucket:
            raise ValueError('Result must belong to the configured object bucket')
        key = reference.get('key')
        size = reference.get('size_bytes')
        if type(size) is not int or not 0 < size <= max_bytes:
            raise ValueError('Result file exceeds the configured Telegram size limit')
        checksum = reference.get('sha256')
        info = self.client.stat_object(self.bucket, key)
        if info.size != size:
            raise ValueError('Stored result size does not match its reference')
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            prefix=config.storage.download_temp_prefix, dir=target.parent,
        )
        response = None
        try:
            try:
                response = self.client.get_object(self.bucket, key)
                digest, downloaded = hashlib.sha256(), 0
                with os.fdopen(descriptor, 'wb') as stream:
                    descriptor = None
                    while True:
                        chunk = response.read(min(
                            config.storage.transfer_chunk_bytes, size - downloaded + 1,
                        ))
                        if not chunk:
                            break
                        downloaded += len(chunk)
                        if downloaded > size or downloaded > max_bytes:
                            raise ValueError('Downloaded result exceeds its declared size limit')
                        digest.update(chunk)
                        stream.write(chunk)
                if downloaded != size or digest.hexdigest() != checksum:
                    raise ValueError('Downloaded result checksum or size does not match its reference')
            finally:
                if response is not None:
                    try:
                        response.close()
                    finally:
                        response.release_conn()
            # The caller's destination survives corrupt or truncated downloads.
            os.replace(temporary, target)
            temporary = None
            return str(path)
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if temporary is not None:
                Path(temporary).unlink(missing_ok=True)
