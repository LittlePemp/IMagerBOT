"""Versioned wire contracts shared by the core and its Telegram adapter.

No imports from either application's `src`, settings or infrastructure.
"""
import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone


SCHEMA_VERSION = 1


@dataclass(frozen=True)
class ObjectRef:
    bucket: str
    key: str
    sha256: str
    size_bytes: int
    content_type: str = 'application/octet-stream'

    def __post_init__(self):
        if not isinstance(self.bucket, str) or not re.fullmatch(r'[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]', self.bucket):
            raise ValueError('Invalid object bucket')
        if (not isinstance(self.key, str) or not self.key or len(self.key) > 1024
                or '\\' in self.key or any(part in ('', '.', '..') for part in self.key.split('/'))):
            raise ValueError('Invalid object key')
        if not isinstance(self.sha256, str) or not re.fullmatch(r'[0-9a-f]{64}', self.sha256):
            raise ValueError('Object SHA256 is required')
        if type(self.size_bytes) is not int or self.size_bytes < 0:
            raise ValueError('Object size must be a nonnegative integer')
        if not isinstance(self.content_type, str) or len(self.content_type) > 128:
            raise ValueError('Invalid object content type')

    @classmethod
    def from_dict(cls, value):
        try:
            return cls(**value)
        except (TypeError, KeyError) as error:
            raise ValueError(f'Invalid object reference: {error}') from error

    def to_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class GenerationParameters:
    group_name: str
    insertion_format: str = 'crop'
    alpha_channel: int = 30
    noise_level: int = 15
    cell_size: int = 40
    result_size: int = 120

    def __post_init__(self):
        if not isinstance(self.group_name, str) or not self.group_name.strip() or len(self.group_name) > 128:
            raise ValueError('Image group must be a nonempty string')
        if self.insertion_format not in ('crop', 'scale'):
            raise ValueError('Invalid insertion format')
        for name in ('alpha_channel', 'noise_level'):
            value = getattr(self, name)
            if type(value) is not int or not 0 <= value <= 100:
                raise ValueError(f'{name} must be an integer between 0 and 100')
        for name in ('cell_size', 'result_size'):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f'{name} must be a positive integer')

    @classmethod
    def from_dict(cls, value):
        try:
            return cls(**value)
        except (TypeError, KeyError) as error:
            raise ValueError(f'Invalid generation parameters: {error}') from error

    def to_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class JobRequest:
    job_id: str
    source: ObjectRef
    parameters: GenerationParameters
    catalog_manifest: ObjectRef
    seed: int
    client_id: str = 'telegram'
    client_context: dict = field(default_factory=dict)
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self):
        if not isinstance(self.job_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', self.job_id):
            raise ValueError('Invalid job ID')
        if not isinstance(self.source, ObjectRef) or not isinstance(self.catalog_manifest, ObjectRef):
            raise ValueError('Source and catalog must be immutable object references')
        if not isinstance(self.parameters, GenerationParameters):
            raise ValueError('Generation parameters are required')
        if type(self.seed) is not int or not 0 <= self.seed < 2**63:
            raise ValueError('Invalid generation seed')
        if not isinstance(self.client_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', self.client_id):
            raise ValueError('Invalid client ID')
        if not isinstance(self.client_context, dict) or len(json.dumps(self.client_context).encode()) > 8192:
            raise ValueError('Invalid client context')
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError('Unsupported job schema version')

    @classmethod
    def from_dict(cls, value):
        try:
            fields = dict(value)
            fields['source'] = ObjectRef.from_dict(fields['source'])
            fields['parameters'] = GenerationParameters.from_dict(fields['parameters'])
            fields['catalog_manifest'] = ObjectRef.from_dict(fields['catalog_manifest'])
            return cls(**fields)
        except (TypeError, KeyError) as error:
            raise ValueError(f'Invalid job request: {error}') from error

    def to_dict(self):
        return asdict(self)


def payload_fingerprint(request):
    value = request.to_dict() if isinstance(request, JobRequest) else request
    encoded = json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(encoded.encode('utf-8')).hexdigest()


def make_submission(request, created_at=None):
    snapshot = (request if isinstance(request, JobRequest) else JobRequest.from_dict(request)).to_dict()
    now = created_at or datetime.now(timezone.utc)
    return {
        '_id': snapshot['job_id'], 'schema_version': SCHEMA_VERSION,
        'request': snapshot, 'payload_hash': payload_fingerprint(snapshot),
        'status': 'queued', 'created_at': now, 'updated_at': now,
        'dispatch': {'state': 'pending', 'token': None, 'lease_until': now,
                     'next_attempt_at': now, 'attempts': 0},
        'execution': {'fence': 0, 'token': None, 'lease_until': now},
        'result': None, 'completion_outbox': None,
    }
