"""Telegram transport failures are independent of rendering and history."""
from dataclasses import dataclass
from enum import Enum
from math import ceil, log
from random import uniform

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


@dataclass(frozen=True)
class DeliveryFailure:
    kind: DeliveryFailureKind
    retry_seconds: float | None
    restore_attempt: bool = False


def bounded_backoff(attempts, initial, coefficient, maximum):
    """Cap the exponent as well as the result for long-lived repair retries."""
    if initial >= maximum or coefficient == 1:
        return min(initial, maximum)
    exponent_cap = ceil(log(maximum / initial, coefficient))
    exponent = min(max(attempts - 1, 0), exponent_cap)
    return min(initial * coefficient ** exponent, maximum)


def delivery_failure(error, attempts, policy):
    if isinstance(error, TelegramRetryAfter):
        return DeliveryFailure(
            DeliveryFailureKind.RATE_LIMITED,
            max(error.retry_after, 0) + policy.notification_retry_after_grace_seconds,
            restore_attempt=True,
        )
    if isinstance(error, (
        TelegramBadRequest, TelegramForbiddenError, TelegramNotFound,
        TelegramEntityTooLarge, TelegramUnauthorizedError,
    )):
        return DeliveryFailure(DeliveryFailureKind.PERMANENT, None)
    if isinstance(error, (ValueError, KeyError, TypeError)):
        return DeliveryFailure(DeliveryFailureKind.CONTRACT, None)
    delay = bounded_backoff(
        attempts, policy.notification_retry_initial_seconds,
        policy.notification_retry_backoff_coefficient,
        policy.notification_retry_max_seconds,
    )
    # Positive jitter never retries earlier than the configured backoff.
    delay = min(
        delay * (1 + uniform(0, policy.notification_retry_jitter_ratio)),
        policy.notification_retry_max_seconds,
    )
    return DeliveryFailure(DeliveryFailureKind.TRANSIENT, delay)
