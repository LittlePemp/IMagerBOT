from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError
from imager_contracts.workflow_versions import legacy_execution_policy_v1


@workflow.defn
class GenerateImageWorkflow:
    """One immutable request and one independently retryable render Activity."""

    @workflow.run
    async def run(self, request: dict, execution_policy: dict | None = None) -> dict:
        # String registration deliberately avoids importing OpenCV, Mongo or
        # MinIO into Temporal's deterministic workflow sandbox.
        legacy = execution_policy is None
        policy = legacy_execution_policy_v1() if legacy else execution_policy
        render = policy['render']
        failure = policy['failure']
        # The legacy history contains exactly one Activity argument. Passing
        # only that argument preserves the recorded payload and allows reset.
        activity_arguments = {'arg': request} if legacy else {'args': [request, render]}
        try:
            return await workflow.execute_activity(
                'render_image',
                **activity_arguments,
                start_to_close_timeout=timedelta(seconds=render['start_to_close_seconds']),
                schedule_to_close_timeout=timedelta(seconds=render['schedule_to_close_seconds']),
                heartbeat_timeout=timedelta(seconds=render['heartbeat_seconds']),
                retry_policy=RetryPolicy(
                    initial_interval=timedelta(seconds=render['retry_initial_seconds']),
                    maximum_interval=timedelta(seconds=render['retry_maximum_seconds']),
                    maximum_attempts=render['maximum_attempts'],
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
                start_to_close_timeout=timedelta(seconds=failure['start_to_close_seconds']),
                schedule_to_close_timeout=timedelta(seconds=failure['schedule_to_close_seconds']),
                retry_policy=RetryPolicy(
                    initial_interval=timedelta(seconds=failure['retry_initial_seconds']),
                    maximum_interval=timedelta(seconds=failure['retry_maximum_seconds']),
                    maximum_attempts=failure['maximum_attempts'],
                ),
            )
            # Mongo may already contain success if only the completion reply
            # was lost. Do not overwrite that canonical result with failure.
            if outcome.get('status') == 'completed':
                return outcome['result']
            raise
