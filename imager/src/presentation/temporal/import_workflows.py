from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError


@workflow.defn
class ImportCatalogWorkflow:
    @workflow.run
    async def run(self, request: dict, execution_policy: dict) -> dict:
        policy = execution_policy['import']
        failure = execution_policy['failure']
        try:
            return await workflow.execute_activity(
                'import_catalog', args=[request, policy],
                start_to_close_timeout=timedelta(seconds=policy['activity_timeout_seconds']),
                schedule_to_close_timeout=timedelta(seconds=policy['schedule_timeout_seconds']),
                heartbeat_timeout=timedelta(seconds=policy['heartbeat_seconds']),
                retry_policy=RetryPolicy(
                    initial_interval=timedelta(seconds=policy['retry_initial_seconds']),
                    maximum_interval=timedelta(seconds=policy['retry_max_seconds']),
                    maximum_attempts=policy['maximum_attempts'],
                    non_retryable_error_types=['CatalogImportRejected'],
                ),
            )
        except ActivityError as error:
            outcome = await workflow.execute_activity(
                'record_import_failure', {'import_id': request['import_id'], 'error': str(error)},
                start_to_close_timeout=timedelta(seconds=failure['timeout_seconds']),
                retry_policy=RetryPolicy(
                    initial_interval=timedelta(seconds=failure['retry_initial_seconds']),
                    maximum_interval=timedelta(seconds=failure['retry_max_seconds']),
                    maximum_attempts=failure['maximum_attempts'],
                ),
            )
            if outcome.get('status') == 'completed':
                return outcome['result']
            raise
