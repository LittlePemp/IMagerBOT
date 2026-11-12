"""Immutable historical policies needed to replay existing Temporal histories.

These values describe commands already recorded by Workflow version 1. Current
submissions receive a TOML policy snapshot instead; this module performs no I/O.
"""
from types import MappingProxyType


LEGACY_RENDER_POLICY_V1 = MappingProxyType({
    'start_to_close_seconds': 900.0,
    'schedule_to_close_seconds': 86400.0,
    'heartbeat_seconds': 30.0,
    'retry_initial_seconds': 2.0,
    'retry_maximum_seconds': 30.0,
    'maximum_attempts': 3,
})
LEGACY_FAILURE_POLICY_V1 = MappingProxyType({
    'start_to_close_seconds': 30.0,
    'schedule_to_close_seconds': 300.0,
    'retry_initial_seconds': 1.0,
    'retry_maximum_seconds': 10.0,
    'maximum_attempts': 5,
})


def legacy_execution_policy_v1() -> dict:
    """Return a detached copy so a caller cannot mutate historical policy."""
    return {
        'render': dict(LEGACY_RENDER_POLICY_V1),
        'failure': dict(LEGACY_FAILURE_POLICY_V1),
    }
