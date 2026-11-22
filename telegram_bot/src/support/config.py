"""Bot-local support limits; no changes to the renderer's configuration."""
import math
import re
import tomllib
from dataclasses import dataclass, fields
from datetime import datetime, timedelta, timezone
from pathlib import Path


DEFAULT_PATH = Path(__file__).resolve().parents[2] / 'config' / 'support.toml'


@dataclass(frozen=True)
class SupportConfig:
    collection: str
    retention_seconds: int
    poll_seconds: float
    max_pending_per_user: int
    max_pending_total: int
    max_stored_events: int
    max_text_length: int
    error_max_length: int
    name_max_length: int


def load_support_config(path=DEFAULT_PATH):
    with Path(path).open('rb') as stream:
        values = tomllib.load(stream)
    expected = {field.name for field in fields(SupportConfig)}
    if set(values) != expected:
        raise ValueError('Support configuration contains unexpected or missing fields')
    if (not isinstance(values['collection'], str)
            or re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*', values['collection']) is None):
        raise ValueError('Support collection must be a plain collection name')
    for name in expected - {'collection', 'poll_seconds'}:
        if type(values[name]) is not int or values[name] <= 0:
            raise ValueError(f'Support {name} must be a positive integer')
    seconds = values['poll_seconds']
    if type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds <= 0:
        raise ValueError('Support poll_seconds must be finite and positive')
    if values['max_text_length'] > 4096 or values['name_max_length'] > 256:
        raise ValueError('Support display limits exceed Telegram message bounds')
    if values['max_pending_per_user'] > values['max_pending_total']:
        raise ValueError('Support per-user inbox cannot exceed its total capacity')
    if values['max_pending_total'] >= values['max_stored_events']:
        raise ValueError('Support archive must leave capacity for pending-message replies')
    try:
        datetime.now(timezone.utc) + timedelta(seconds=values['retention_seconds'])
    except (OverflowError, ValueError) as error:
        raise ValueError('Support retention exceeds the supported timestamp range') from error
    values['poll_seconds'] = float(seconds)
    return SupportConfig(**values)


support_config = load_support_config()
