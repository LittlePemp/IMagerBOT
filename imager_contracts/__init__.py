"""Versioned wire contracts shared by the core and its Telegram adapter.

No imports from either application's `src`, settings or infrastructure.
"""
import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from .protocol import (
    BINARY_CONTENT_TYPE, BUCKET_PATTERN, CLIENT_ID_PATTERN, CONTENT_TYPE_MAX_LENGTH,
    CONTEXT_MAX_BYTES, GROUP_NAME_MAX_LENGTH, INSERTION_FORMATS, JOB_ID_PATTERN,
    OBJECT_KEY_MAX_LENGTH, PERCENT_MAX, SCHEMA_VERSION, SEED_UPPER_BOUND, SHA256_PATTERN,
    WATERMARK_TEXT_MAX_LENGTH,
)


def _generation_default(name):
    # Wire definitions remain importable without configuration/filesystem I/O.
    from imager_config.loader import config
    return getattr(config.generation, name)


def validate_watermark_text(value):
    """Optional generic branding, independent of a transport or URL service."""
    if value is None:
        return None
    if (not isinstance(value, str) or not 0 < len(value) <= WATERMARK_TEXT_MAX_LENGTH
            or not value.isascii() or not value.isprintable() or value.isspace()):
        raise ValueError('Watermark must be bounded, nonempty, single-line printable ASCII text')
    return value


@dataclass(frozen=True)
class ObjectRef:
    bucket: str
    key: str
    sha256: str
    size_bytes: int
    content_type: str = BINARY_CONTENT_TYPE

    def __post_init__(self):
        if not isinstance(self.bucket, str) or not re.fullmatch(BUCKET_PATTERN, self.bucket):
            raise ValueError('Invalid object bucket')
        if (not isinstance(self.key, str) or not self.key or len(self.key) > OBJECT_KEY_MAX_LENGTH
                or '\\' in self.key or any(part in ('', '.', '..') for part in self.key.split('/'))):
            raise ValueError('Invalid object key')
        if not isinstance(self.sha256, str) or not re.fullmatch(SHA256_PATTERN, self.sha256):
            raise ValueError('Object SHA256 is required')
        if type(self.size_bytes) is not int or self.size_bytes < 0:
            raise ValueError('Object size must be a nonnegative integer')
        if not isinstance(self.content_type, str) or len(self.content_type) > CONTENT_TYPE_MAX_LENGTH:
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
    insertion_format: str = field(default_factory=lambda: _generation_default('insertion_format'))
    alpha_channel: int = field(default_factory=lambda: _generation_default('alpha_channel'))
    noise_level: int = field(default_factory=lambda: _generation_default('noise_level'))
    cell_size: int = field(default_factory=lambda: _generation_default('cell_size'))
    result_size: int = field(default_factory=lambda: _generation_default('result_size'))

    def __post_init__(self):
        if not isinstance(self.group_name, str) or not self.group_name.strip() or len(self.group_name) > GROUP_NAME_MAX_LENGTH:
            raise ValueError('Image group must be a nonempty string')
        if self.insertion_format not in INSERTION_FORMATS:
            raise ValueError('Invalid insertion format')
        for name in ('alpha_channel', 'noise_level'):
            value = getattr(self, name)
            if type(value) is not int or not 0 <= value <= PERCENT_MAX:
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
    watermark_text: str | None = None

    def __post_init__(self):
        if not isinstance(self.job_id, str) or not re.fullmatch(JOB_ID_PATTERN, self.job_id):
            raise ValueError('Invalid job ID')
        if not isinstance(self.source, ObjectRef) or not isinstance(self.catalog_manifest, ObjectRef):
            raise ValueError('Source and catalog must be immutable object references')
        if not isinstance(self.parameters, GenerationParameters):
            raise ValueError('Generation parameters are required')
        if type(self.seed) is not int or not 0 <= self.seed < SEED_UPPER_BOUND:
            raise ValueError('Invalid generation seed')
        if not isinstance(self.client_id, str) or not re.fullmatch(CLIENT_ID_PATTERN, self.client_id):
            raise ValueError('Invalid client ID')
        if not isinstance(self.client_context, dict) or len(json.dumps(self.client_context).encode()) > CONTEXT_MAX_BYTES:
            raise ValueError('Invalid client context')
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError('Unsupported job schema version')
        validate_watermark_text(self.watermark_text)

    @classmethod
    def from_dict(cls, value):
        try:
            fields = dict(value)
            # BSON Int64 is an int subclass. Canonicalize its representation
            # without accepting bool, floats or strings as integer seeds.
            seed = fields.get('seed')
            if isinstance(seed, int) and not isinstance(seed, bool):
                fields['seed'] = int(seed)
            fields['source'] = ObjectRef.from_dict(fields['source'])
            fields['parameters'] = GenerationParameters.from_dict(fields['parameters'])
            fields['catalog_manifest'] = ObjectRef.from_dict(fields['catalog_manifest'])
            return cls(**fields)
        except (TypeError, KeyError) as error:
            raise ValueError(f'Invalid job request: {error}') from error

    def to_dict(self):
        result = asdict(self)
        # Preserve the exact canonical representation of already accepted jobs.
        # Adding a null field would otherwise invalidate their stored hashes.
        if self.watermark_text is None:
            result.pop('watermark_text')
        return result


def payload_fingerprint(request):
    value = request.to_dict() if isinstance(request, JobRequest) else request
    if isinstance(value, dict) and value.get('watermark_text') is None:
        value = {key: item for key, item in value.items() if key != 'watermark_text'}
    encoded = json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(encoded.encode('utf-8')).hexdigest()


def make_submission(request, created_at=None):
    from imager_config.loader import config
    snapshot = (request if isinstance(request, JobRequest) else JobRequest.from_dict(request)).to_dict()
    now = created_at or datetime.now(timezone.utc)
    return {
        '_id': snapshot['job_id'], 'schema_version': SCHEMA_VERSION,
        'request': snapshot, 'payload_hash': payload_fingerprint(snapshot),
        'execution_policy': {'render': asdict(config.temporal.render),
                             'failure': asdict(config.temporal.failure)},
        'status': 'queued', 'created_at': now, 'updated_at': now,
        'dispatch': {'state': 'pending', 'token': None, 'lease_until': now,
                     'next_attempt_at': now, 'attempts': 0},
        'execution': {'fence': 0, 'token': None, 'lease_until': now},
        'result': None, 'completion_outbox': None,
    }
