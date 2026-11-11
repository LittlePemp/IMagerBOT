"""Temporal adapter: sync rendering with a renewable external execution claim."""
import contextvars
import threading
import time

from temporalio import activity
from temporalio.exceptions import ApplicationError

from imager_contracts import JobRequest
from src.infrastructure.jobs.mongo_jobs import JobBusy, LostExecution
from src.shared_kernel.loggers import presentation_logger


class LeaseHeartbeat:
    def __init__(self, repository, job_id, token, interval=5):
        self.repository, self.job_id, self.token = repository, job_id, token
        self.interval = interval
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.details = {'job_id': job_id, 'stage': 'starting'}
        self.failure = None

    def pulse(self):
        if activity.is_cancelled():
            raise LostExecution('Temporal cancelled the previous attempt')
        with self.lock:
            details = dict(self.details)
        self.repository.heartbeat_execution(self.job_id, self.token, details)
        activity.heartbeat(details)

    def progress(self, details):
        self.check()
        with self.lock:
            self.details = dict(details)

    def check(self):
        if self.failure is not None:
            raise LostExecution(f'Cannot maintain execution ownership: {self.failure}')
        if activity.is_cancelled():
            raise LostExecution('Temporal requested cancellation of this attempt')

    def __enter__(self):
        self.pulse()
        context = contextvars.copy_context()

        def run():
            while not self.stop.wait(self.interval):
                try:
                    self.pulse()
                except Exception as error:
                    presentation_logger.exception(f'Execution heartbeat failed: job_id={self.job_id}')
                    self.failure = error
                    return

        self.thread = threading.Thread(
            target=lambda: context.run(run), name='imager-lease-heartbeat', daemon=True,
        )
        self.thread.start()
        return self

    def __exit__(self, *exception):
        self.stop.set()
        self.thread.join(timeout=15)


class RenderActivities:
    MAX_ATTEMPTS = 3

    def __init__(self, repository, executor):
        self.repository, self.executor = repository, executor

    @activity.defn(name='render_image')
    def render_image(self, request: dict) -> dict:
        token = None
        job_id = request.get('job_id', 'invalid') if isinstance(request, dict) else 'invalid'
        try:
            snapshot = JobRequest.from_dict(request).to_dict()
            while True:
                try:
                    claim = self.repository.claim_execution(snapshot)
                    break
                except JobBusy:
                    # A timed-out attempt may still own its external lease.
                    # Wait with heartbeats, rather than burning all retries in
                    # a few seconds before that lease can expire.
                    if activity.is_cancelled():
                        raise LostExecution('Cancelled while waiting for an execution lease')
                    activity.heartbeat({'job_id': job_id, 'stage': 'waiting_for_execution_lease'})
                    time.sleep(2)
            if claim['already_completed']:
                return claim['result']
            token = claim['token']
            with LeaseHeartbeat(self.repository, job_id, token) as heartbeat:
                reference = self.executor.execute(snapshot, token, heartbeat.progress)
                heartbeat.check()
            reference = self.repository.complete(job_id, token, reference)
            presentation_logger.info(f'Render completed: job_id={job_id}, fence={claim["fence"]}')
            return reference
        except Exception as error:
            presentation_logger.exception(f'Render Activity failed: job_id={job_id}')
            retryable = bool(getattr(error, 'retryable', False))
            # Requests and renderer policy failures must not be retried. Unknown
            # infrastructure failures get bounded retries, never silent success.
            if not isinstance(error, (ValueError, ApplicationError)) and not hasattr(error, 'retryable'):
                retryable = True
            attempt = activity.info().attempt
            if token is not None:
                if retryable and attempt < self.MAX_ATTEMPTS:
                    self.repository.retry_execution(job_id, token, error)
                else:
                    self.repository.fail(job_id, token, error)
            raise ApplicationError(
                str(error), type='TransientRenderError' if retryable else 'RenderRejected',
                non_retryable=not retryable,
            ) from error

    @activity.defn(name='record_job_failure')
    def record_job_failure(self, request: dict) -> dict:
        return self.repository.finalize_failure(request['job_id'], request.get('error', 'Render failed'))
