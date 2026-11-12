import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from temporalio.service import RPCError, RPCStatusCode
from src.presentation.temporal.namespace import ensure_namespace


class TestNamespaceBootstrap(unittest.IsolatedAsyncioTestCase):
    def client(self, error=None):
        return SimpleNamespace(workflow_service=SimpleNamespace(
            describe_namespace=AsyncMock(side_effect=error), register_namespace=AsyncMock(),
        ))

    async def test_existing_namespace_preserves_retention(self):
        client = self.client()
        self.assertFalse(await ensure_namespace(client, 'imager', 7))
        client.workflow_service.register_namespace.assert_not_awaited()

    async def test_missing_namespace_registers_retention(self):
        client = self.client(RPCError('missing', RPCStatusCode.NOT_FOUND, b''))
        self.assertTrue(await ensure_namespace(client, 'imager', 7))
        request = client.workflow_service.register_namespace.call_args.args[0]
        self.assertEqual(request.namespace, 'imager')
        self.assertEqual(request.workflow_execution_retention_period.seconds, 7 * 24 * 60 * 60)

    async def test_transport_failure_is_not_treated_as_missing_namespace(self):
        client = self.client(RPCError('offline', RPCStatusCode.UNAVAILABLE, b''))
        with self.assertRaises(RPCError):
            await ensure_namespace(client, 'imager', 7)
        client.workflow_service.register_namespace.assert_not_awaited()

    async def test_concurrent_namespace_creation_is_idempotent(self):
        client = self.client(RPCError('missing', RPCStatusCode.NOT_FOUND, b''))
        client.workflow_service.register_namespace.side_effect = RPCError(
            'created by another initializer', RPCStatusCode.ALREADY_EXISTS, b'',
        )
        self.assertFalse(await ensure_namespace(client, 'imager', 7))
