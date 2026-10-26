"""Runtime value objects shared by the worker and persistence layer."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class AttemptLease:
    """The identity a worker presents for every lifecycle write."""

    attempt_id: str
    task_id: str
    attempt_no: int
    worker_id: str
    lease_token: str
    fencing_epoch: int
    lease_expires_at: datetime | None = None
    trace_id: str | None = None


@dataclass(frozen=True)
class StepRecord:
    step_key: str
    graph_node_key: str
    status: str
    result: dict | None
    result_hash: str | None
    owning_attempt_id: str | None
    fencing_epoch: int | None
