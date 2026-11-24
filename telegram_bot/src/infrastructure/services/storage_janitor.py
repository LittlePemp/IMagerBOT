"""Periodic removal of files the bot owns but no longer references.

* Local Bot API cache: telegram-bot-api (--local) keeps every downloaded photo
  and ZIP forever. Transferred files are released immediately; this sweep
  removes whatever a crash or an abandoned conversation left behind.
* Unadmitted sources: a source is uploaded to MinIO before its admission is
  stored. If the process stops in between, no job document ever points at the
  object, so the core retention cannot see it.
* Scratch: temporary directories of a killed process in the bind-mounted /tmp.
"""
import asyncio
import tempfile
from contextlib import suppress
from datetime import datetime, timedelta, timezone

from imager_config.loader import config
from imager_contracts.scratch import purge_stale_entries, purge_telegram_cache
from src.infrastructure.services.object_storage import BotObjectStorage
from src.utils.loggers import exception_logger, service_logger


def bot_scratch_prefixes():
    return (config.storage.upload_temp_prefix, config.storage.result_temp_prefix,
            config.pack_import.temp_prefix)


def purge_bot_scratch(root=None):
    return purge_stale_entries(
        root or tempfile.gettempdir(), bot_scratch_prefixes(), config.storage.scratch_max_age_seconds,
    )


class StorageJanitor:
    def __init__(self, gateway, admissions, *, local_api, clock=None):
        self.gateway, self.admissions = gateway, admissions
        self.local_api = local_api
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    async def sweep_telegram_cache(self):
        if not self.local_api:
            return 0, 0
        return await asyncio.to_thread(
            purge_telegram_cache, config.pack_import.bot_api_container_directory,
            config.bot.telegram_cache_max_age_seconds,
        )

    async def sweep_orphan_sources(self):
        cutoff = self.clock() - timedelta(seconds=config.bot.orphan_source_grace_seconds)
        removed = 0
        for key, modified in await self.gateway.list_sources():
            if modified is None or modified > cutoff:
                continue
            job_id = BotObjectStorage.source_job_id(key)
            if job_id is None:
                continue
            # Referenced by a pending admission or by a core job: not an orphan.
            if await self.admissions.find(job_id) is not None:
                continue
            if await self.gateway.get_job(job_id) is not None:
                continue
            await self.gateway.delete_source(job_id, key)
            removed += 1
        return removed

    async def run_once(self):
        report = {}
        for name, step in (('telegram_cache', self.sweep_telegram_cache),
                           ('orphan_sources', self.sweep_orphan_sources),
                           ('scratch', lambda: asyncio.to_thread(purge_bot_scratch))):
            try:
                report[name] = await step()
            except asyncio.CancelledError:
                raise
            except Exception:
                exception_logger.exception(f'Storage janitor step failed: {name}')
        service_logger.info(f'Storage janitor finished: {report}')
        return report

    async def run(self, stop):
        while not stop.is_set():
            await self.run_once()
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=config.bot.janitor_poll_seconds)
