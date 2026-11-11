"""Telegram's S3 boundary. Files on disk are temporary transfer buffers only."""
import hashlib
import os
from pathlib import Path


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
                    timeout=urllib3.Timeout(connect=5, read=30),
                    retries=urllib3.Retry(total=2, backoff_factor=0.2),
                ),
            )
        self.client = client
        self.bucket = bucket

    @staticmethod
    def sha256_file(path):
        digest = hashlib.sha256()
        with open(path, 'rb') as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(chunk)
        return digest.hexdigest()

    def upload_source(self, path, job_id, *, max_bytes=64_000_000):
        size = os.path.getsize(path)
        if not 0 < size <= max_bytes:
            raise ValueError(f'Source file must contain between 1 and {max_bytes} bytes')
        if not job_id or any(character not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_' for character in job_id):
            raise ValueError('Invalid job identifier')
        checksum = self.sha256_file(path)
        key = f'uploads/{job_id}/{checksum}.jpg'
        if not self.client.bucket_exists(self.bucket):
            try:
                self.client.make_bucket(self.bucket)
            except Exception as error:
                if getattr(error, 'code', '') not in ('BucketAlreadyExists', 'BucketAlreadyOwnedByYou'):
                    raise
        self.client.fput_object(
            self.bucket, key, str(path), content_type='image/jpeg',
            metadata={'sha256': checksum}, num_parallel_uploads=1,
        )
        return {
            'bucket': self.bucket, 'key': key, 'sha256': checksum,
            'size_bytes': size, 'content_type': 'image/jpeg',
        }

    def download_result(self, reference, path, *, max_bytes):
        if reference.get('bucket') != self.bucket:
            raise ValueError('Result must belong to the configured object bucket')
        key = reference.get('key')
        if not isinstance(key, str) or not key.startswith('jobs/'):
            raise ValueError('Invalid result object key')
        size = reference.get('size_bytes')
        if type(size) is not int or not 0 < size <= max_bytes:
            raise ValueError('Result file exceeds the configured Telegram size limit')
        checksum = reference.get('sha256')
        if not isinstance(checksum, str) or len(checksum) != 64:
            raise ValueError('Result checksum is missing')
        info = self.client.stat_object(self.bucket, key)
        if info.size != size:
            raise ValueError('Stored result size does not match its reference')
        self.client.fget_object(self.bucket, key, str(path))
        if os.path.getsize(path) != size or self.sha256_file(path) != checksum:
            Path(path).unlink(missing_ok=True)
            raise ValueError('Downloaded result checksum does not match its reference')
        return str(path)
