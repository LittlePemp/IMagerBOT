"""Durable bot receipt, independent of the disposable history projection."""
from dataclasses import dataclass


@dataclass(frozen=True)
class AdmissionReceipt:
    job_id: str
    state: str
    date: str
    payload_hash: str
    duplicate: bool = False
    actor_id: int | None = None


@dataclass(frozen=True)
class AdmissionClaim:
    job_id: str
    token: str
    document: dict
