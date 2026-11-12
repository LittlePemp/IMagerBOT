import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from pydantic import ValidationError
from temporalio.client import TLSConfig

from settings import Settings
from src.infrastructure import temporal_client


class TemporalSettingsMixin:
    @staticmethod
    def configuration(**values):
        defaults = {
            'mongodb_uri': 'mongodb://127.0.0.1:1',
            'temporal_address': 'temporal.example.test:7233',
            'temporal_namespace': 'test-namespace',
            'temporal_tls_enabled': False,
            'temporal_tls_root_ca_cert': '',
            'temporal_tls_client_cert': '',
            'temporal_tls_client_key': '',
            'temporal_tls_server_name': '',
        }
        return Settings(_env_file=None, **(defaults | values))


class TestTemporalClient(TemporalSettingsMixin, unittest.IsolatedAsyncioTestCase):
    async def test_plain_connection_explicitly_passes_no_tls(self):
        configuration = self.configuration()
        self.assertIsNone(temporal_client.temporal_tls(configuration))
        connected = object()
        with patch.object(temporal_client.Client, 'connect',
                          AsyncMock(return_value=connected)) as connect:
            result = await temporal_client.connect_temporal_client(configuration)
        self.assertIs(result, connected)
        connect.assert_awaited_once_with(
            configuration.temporal_address,
            namespace=configuration.temporal_namespace,
            tls=None,
        )

    async def test_mtls_reads_credentials_and_forwards_server_name(self):
        material = {
            'temporal_tls_root_ca_cert': ('ca.pem', b'test-root-ca'),
            'temporal_tls_client_cert': ('client.pem', b'test-client-certificate'),
            'temporal_tls_client_key': ('client.key', b'test-private-key'),
        }
        with tempfile.TemporaryDirectory(prefix='imager-temporal-tls-test-') as directory:
            paths = {}
            for field, (name, contents) in material.items():
                path = Path(directory) / name
                path.write_bytes(contents)
                paths[field] = str(path)
            configuration = self.configuration(
                temporal_tls_enabled=True,
                temporal_tls_server_name='temporal.example.test', **paths,
            )
            connected = object()
            with patch.object(temporal_client.Client, 'connect',
                              AsyncMock(return_value=connected)) as connect:
                result = await temporal_client.connect_temporal_client(configuration)
        self.assertIs(result, connected)
        connect.assert_awaited_once()
        self.assertEqual(connect.await_args.args, (configuration.temporal_address,))
        self.assertEqual(connect.await_args.kwargs['namespace'], configuration.temporal_namespace)
        tls = connect.await_args.kwargs['tls']
        self.assertIsInstance(tls, TLSConfig)
        self.assertEqual(tls.server_root_ca_cert, material['temporal_tls_root_ca_cert'][1])
        self.assertEqual(tls.client_cert, material['temporal_tls_client_cert'][1])
        self.assertEqual(tls.client_private_key, material['temporal_tls_client_key'][1])
        self.assertEqual(tls.domain, configuration.temporal_tls_server_name)

    async def test_missing_credential_file_fails_before_connect(self):
        with tempfile.TemporaryDirectory(prefix='imager-temporal-tls-test-') as directory:
            configuration = self.configuration(
                temporal_tls_enabled=True,
                temporal_tls_root_ca_cert=str(Path(directory) / 'missing-ca.pem'),
                temporal_tls_client_cert=str(Path(directory) / 'client.pem'),
                temporal_tls_client_key=str(Path(directory) / 'client.key'),
                temporal_tls_server_name='temporal.example.test',
            )
            with patch.object(temporal_client.Client, 'connect', AsyncMock()) as connect:
                with self.assertRaises(FileNotFoundError):
                    await temporal_client.connect_temporal_client(configuration)
            connect.assert_not_awaited()


class TestTemporalTlsSettings(TemporalSettingsMixin, unittest.TestCase):
    def test_enabled_tls_rejects_each_missing_material_field(self):
        complete = {
            'temporal_tls_enabled': True,
            'temporal_tls_root_ca_cert': 'ca.pem',
            'temporal_tls_client_cert': 'client.pem',
            'temporal_tls_client_key': 'client.key',
            'temporal_tls_server_name': 'temporal.example.test',
        }
        for field in complete.keys() - {'temporal_tls_enabled'}:
            with self.subTest(field=field), self.assertRaises(ValidationError):
                self.configuration(**(complete | {field: ''}))

    def test_environment_cannot_disable_tls_and_leave_material_configured(self):
        environment = {
            'TEMPORAL_TLS_ENABLED': 'false',
            'TEMPORAL_TLS_ROOT_CA_CERT': 'ca.pem',
            'TEMPORAL_TLS_CLIENT_CERT': 'client.pem',
            'TEMPORAL_TLS_CLIENT_KEY': 'client.key',
            'TEMPORAL_TLS_SERVER_NAME': 'temporal.example.test',
        }
        with patch.dict(os.environ, environment):
            with self.assertRaises(ValidationError):
                Settings(_env_file=None, mongodb_uri='mongodb://127.0.0.1:1')
