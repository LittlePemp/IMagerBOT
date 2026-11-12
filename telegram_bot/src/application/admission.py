"""Replay accepted immutable bot submissions into the core inbox."""
import asyncio
from contextlib import nullcontext, suppress

from imager_config.loader import config
from src.utils.loggers import exception_logger, service_logger


class AdmissionDispatcher:
    def __init__(self, repository, gateway, *, publication_lock=None):
        self.repository = repository
        self.gateway = gateway
        self.publication_lock = publication_lock

    async def _renew(self, claim, lost):
        interval = self.repository.lease_seconds / config.bot.notification_renew_divisor
        while True:
            await asyncio.sleep(interval)
            try:
                if not await self.repository.renew(claim.job_id, claim.token):
                    lost.set()
                    return
            except Exception:
                lost.set()
                exception_logger.exception(f'Cannot renew bot admission: job_id={claim.job_id}')
                return

    async def deliver_once(self):
        # Shared with the single polling process's backlog check + acceptance.
        # It closes the count/publish/ack observation window without pretending
        # to coordinate independent bot replicas.
        async with self.publication_lock if self.publication_lock is not None else nullcontext():
            return await self._deliver_once()

    async def _deliver_once(self):
        claim = await self.repository.claim()
        if claim is None:
            return False
        lost = asyncio.Event()
        renewal = asyncio.create_task(self._renew(claim, lost))
        try:
            await self.gateway.publish_submission(claim.document)
            if lost.is_set() or not await self.repository.acknowledge(claim.job_id, claim.token):
                raise RuntimeError('Bot admission publication lease was lost')
            service_logger.info(f'Bot admission published: job_id={claim.job_id}')
        except asyncio.CancelledError:
            raise
        except Exception as error:
            exception_logger.exception(f'Bot admission publication failed: job_id={claim.job_id}')
            await self.repository.retry(
                claim.job_id, claim.token, error, error_max_length=config.bot.error_max_length,
            )
        finally:
            renewal.cancel()
            with suppress(asyncio.CancelledError):
                await renewal
        return True

    async def run(self, stop):
        while not stop.is_set():
            try:
                if await self.deliver_once():
                    continue
            except asyncio.CancelledError:
                raise
            except Exception:
                exception_logger.exception('Bot admission outbox polling failed')
            try:
                await asyncio.wait_for(stop.wait(), timeout=config.bot.admission_poll_seconds)
            except asyncio.TimeoutError:
                pass
