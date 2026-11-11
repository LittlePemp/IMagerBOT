"""Errors retain a readable message and retry information across adapters."""
from pymongo.errors import AutoReconnect, ConnectionFailure, ExecutionTimeout


class OperationError(str):
    def __new__(cls, message, *, code='operation_failed', retryable=False):
        instance = super().__new__(cls, str(message))
        instance.code = code
        instance.retryable = retryable
        return instance


def error_from_exception(error, context='Operation failed'):
    return OperationError(
        f'{context}: {error}',
        retryable=bool(getattr(error, 'retryable', False)) or isinstance(
            error, (ConnectionError, TimeoutError, AutoReconnect,
                    ConnectionFailure, ExecutionTimeout),
        ),
    )
