from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError


@workflow.defn
class GenerateImageWorkflow:
    """One immutable request and one independently retryable render Activity."""

    @workflow.run
    async def run(self, request: dict) -> dict:
        # String registration deliberately avoids importing OpenCV, Mongo or
        # MinIO into Temporal's deterministic workflow sandbox.
        try:
            return await workflow.execute_activity(
                'render_image',
                request,
                start_to_close_timeout=timedelta(minutes=15),
                schedule_to_close_timeout=timedelta(hours=24),
                heartbeat_timeout=timedelta(seconds=30),
                retry_policy=RetryPolicy(
                    initial_interval=timedelta(seconds=2),
                    maximum_interval=timedelta(seconds=30),
                    maximum_attempts=3,
                    non_retryable_error_types=[
                        'InvalidGenerationRequest', 'GenerationRejected',
                        'ResourceLimitExceeded', 'JobConflict',
                    ],
                ),
            )
        except ActivityError as error:
            # Timeouts/OOM can bypass the Activity's own exception handler.
            # Record final failure through another durable, idempotent Activity.
            outcome = await workflow.execute_activity(
                'record_job_failure',
                {'job_id': request['job_id'], 'error': str(error)},
                start_to_close_timeout=timedelta(seconds=30),
                schedule_to_close_timeout=timedelta(minutes=5),
                retry_policy=RetryPolicy(
                    initial_interval=timedelta(seconds=1),
                    maximum_interval=timedelta(seconds=10),
                    maximum_attempts=5,
                ),
            )
            # Mongo may already contain success if only the completion reply
            # was lost. Do not overwrite that canonical result with failure.
            if outcome.get('status') == 'completed':
                return outcome['result']
            raise
