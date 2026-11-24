"""Serialize source publication and cleanup in the single polling process."""
import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field


@dataclass
class _SourceEntry:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    users: int = 0


class SourceLifecycle:
    def __init__(self):
        self._entries = {}

    @asynccontextmanager
    async def protect(self, job_id):
        if not isinstance(job_id, str) or not job_id:
            raise ValueError('A source needs a job identity')
        entry = self._entries.setdefault(job_id, _SourceEntry())
        entry.users += 1
        try:
            async with entry.lock:
                yield
        finally:
            entry.users -= 1
            if not entry.users:
                del self._entries[job_id]
