"""Idempotent namespace bootstrap using the same verified mTLS client as workers."""
import asyncio
from datetime import timedelta

from google.protobuf.duration_pb2 import Duration
from temporalio.api.workflowservice.v1 import DescribeNamespaceRequest, RegisterNamespaceRequest
from temporalio.service import RPCError, RPCStatusCode

from imager_config.loader import config
from settings import settings
from src.infrastructure.temporal_client import connect_temporal_client
from src.shared_kernel.loggers import presentation_logger


async def ensure_namespace(client, namespace, retention_days):
    try:
        await client.workflow_service.describe_namespace(DescribeNamespaceRequest(namespace=namespace))
        return False
    except RPCError as error:
        if error.status != RPCStatusCode.NOT_FOUND:
            raise
    retention = Duration()
    retention.FromTimedelta(timedelta(days=retention_days))
    try:
        await client.workflow_service.register_namespace(RegisterNamespaceRequest(
            namespace=namespace, workflow_execution_retention_period=retention,
        ))
        return True
    except RPCError as error:
        if error.status != RPCStatusCode.ALREADY_EXISTS:
            raise
        return False


async def main():
    policy = config.deployment.production
    for attempt in range(policy.namespace_startup_attempts):
        try:
            client = await connect_temporal_client(settings)
            created = await ensure_namespace(client, settings.temporal_namespace,
                                             policy.namespace_retention_days)
            presentation_logger.info(
                f'Temporal namespace ready: namespace={settings.temporal_namespace}, created={created}'
            )
            return
        except Exception:
            presentation_logger.exception('Temporal namespace bootstrap is not ready')
            if attempt + 1 == policy.namespace_startup_attempts:
                raise
            await asyncio.sleep(policy.namespace_retry_seconds)


if __name__ == '__main__':
    asyncio.run(main())
