"""Pure immutable transport contract for administrator catalog imports."""
import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

from . import ObjectRef
from .protocol import GROUP_NAME_MAX_LENGTH, JOB_ID_PATTERN, SCHEMA_VERSION

ZIP_CONTENT_TYPE = 'application/zip'


@dataclass(frozen=True)
class CatalogImportRequest:
    import_id: str
    actor_id: int
    group_name: str
    display_name: str
    source: ObjectRef
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self):
        if not isinstance(self.import_id, str) or not re.fullmatch(JOB_ID_PATTERN, self.import_id):
            raise ValueError('Invalid import identifier')
        if type(self.actor_id) is not int or self.actor_id <= 0:
            raise ValueError('Invalid import actor')
        if (not isinstance(self.group_name, str) or not re.fullmatch(JOB_ID_PATTERN, self.group_name)):
            raise ValueError('Invalid catalog identifier')
        if (not isinstance(self.display_name, str) or not self.display_name
                or self.display_name != self.display_name.strip()
                or len(self.display_name) > GROUP_NAME_MAX_LENGTH
                or any(ord(char) < 32 or ord(char) == 127 for char in self.display_name)):
            raise ValueError('Catalog name must be nonempty text without control characters')
        try:
            self.display_name.encode('utf-8')
        except UnicodeEncodeError as error:
            raise ValueError('Catalog name must be valid Unicode') from error
        if (not isinstance(self.source, ObjectRef) or self.source.size_bytes <= 0
                or self.source.content_type != ZIP_CONTENT_TYPE):
            raise ValueError('A nonempty ZIP object reference is required')
        if type(self.schema_version) is not int or self.schema_version != SCHEMA_VERSION:
            raise ValueError('Unsupported import schema version')

    @classmethod
    def from_dict(cls, value):
        try:
            fields = dict(value)
            fields['source'] = ObjectRef.from_dict(fields['source'])
            return cls(**fields)
        except (KeyError, TypeError) as error:
            raise ValueError('Invalid catalog import request') from error

    def to_dict(self):
        return asdict(self)


def import_fingerprint(request):
    snapshot = request.to_dict() if isinstance(request, CatalogImportRequest) else CatalogImportRequest.from_dict(request).to_dict()
    data = json.dumps(snapshot, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(data.encode('utf-8')).hexdigest()


def make_import_submission(request, created_at=None):
    from imager_config.loader import config
    snapshot = (request if isinstance(request, CatalogImportRequest) else CatalogImportRequest.from_dict(request)).to_dict()
    policy = config.pack_import
    now = created_at or datetime.now(timezone.utc)
    return {
        '_id': snapshot['import_id'], 'schema_version': SCHEMA_VERSION,
        'request': snapshot, 'payload_hash': import_fingerprint(snapshot),
        'execution_policy': {
            'import': {name: getattr(policy, name) for name in (
                'activity_timeout_seconds', 'schedule_timeout_seconds', 'heartbeat_seconds',
                'retry_initial_seconds', 'retry_max_seconds', 'maximum_attempts',
            )},
            'failure': {'timeout_seconds': policy.failure_timeout_seconds,
                        'retry_initial_seconds': policy.retry_initial_seconds,
                        'retry_max_seconds': policy.retry_max_seconds,
                        'maximum_attempts': policy.maximum_attempts},
        },
        'status': 'queued', 'created_at': now, 'updated_at': now,
        'dispatch': {'state': 'pending', 'token': None, 'lease_until': now,
                     'next_attempt_at': now, 'attempts': 0,
                     'workflow_id': f'{policy.workflow_id_prefix}{snapshot["import_id"]}'},
        'execution': {'fence': 0, 'token': None, 'lease_until': now},
        'progress': {'phase': 'queued', 'processed': 0, 'total': 0},
        'result': None, 'error': None,
    }
