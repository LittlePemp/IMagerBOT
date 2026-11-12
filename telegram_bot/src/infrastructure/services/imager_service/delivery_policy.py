"""Telegram transport failures are independent of rendering and history."""
from enum import Enum
from math import ceil, log

from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramEntityTooLarge,
    TelegramForbiddenError,
    TelegramNotFound,
    TelegramRetryAfter,
    TelegramUnauthorizedError,
)


class DeliveryFailureKind(str, Enum):
    RATE_LIMITED = 'rate_limited'
    PERMANENT = 'permanent'
    CONTRACT = 'contract'
    TRANSIENT = 'transient'


def bounded_backoff(attempts, initial, coefficient, maximum):
    """Cap the exponent as well as the result for long-lived repair retries."""
    if initial >= maximum or coefficient == 1:
        return min(initial, maximum)
    exponent_cap = ceil(log(maximum / initial, coefficient))
    exponent = min(max(attempts - 1, 0), exponent_cap)
    return min(initial * coefficient ** exponent, maximum)


def delivery_failure(error):
    """Classify an error without scheduling another Telegram send."""
    if isinstance(error, TelegramRetryAfter):
        return DeliveryFailureKind.RATE_LIMITED
    if isinstance(error, (
        TelegramBadRequest, TelegramForbiddenError, TelegramNotFound,
        TelegramEntityTooLarge, TelegramUnauthorizedError,
    )):
        return DeliveryFailureKind.PERMANENT
    if isinstance(error, (ValueError, KeyError, TypeError)):
        return DeliveryFailureKind.CONTRACT
    return DeliveryFailureKind.TRANSIENT
