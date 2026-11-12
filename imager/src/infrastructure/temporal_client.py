"""One connection path for workers, dispatcher and maintenance commands."""
from pathlib import Path

from temporalio.client import Client, TLSConfig


def temporal_tls(configuration):
    if not configuration.temporal_tls_enabled:
        return None
    # Credentials are read at the composition boundary, outside Workflow code.
    return TLSConfig(
        server_root_ca_cert=Path(configuration.temporal_tls_root_ca_cert).read_bytes(),
        client_cert=Path(configuration.temporal_tls_client_cert).read_bytes(),
        client_private_key=Path(configuration.temporal_tls_client_key).read_bytes(),
        domain=configuration.temporal_tls_server_name,
    )


async def connect_temporal_client(configuration=None):
    if configuration is None:
        from settings import settings
        configuration = settings
    return await Client.connect(
        configuration.temporal_address, namespace=configuration.temporal_namespace,
        tls=temporal_tls(configuration),
    )
