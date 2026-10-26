"""Deterministic hooks at durable lifecycle crash windows."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Protocol


class FaultPoint(StrEnum):
    TASK_CREATE_BEFORE_COMMIT = "task.create.before_commit"
    TASK_CREATE_AFTER_COMMIT = "task.create.after_commit"
    ATTEMPT_CLAIMED = "attempt.claimed"
    MODEL_CALL_IN_FLIGHT = "model.call.in_flight"
    MODEL_RETURNED = "model.returned"
    TOOL_RESULT_BEFORE_PERSIST = "tool.result.before_persist"
    TOOL_RESULT_BEFORE_COMMIT = "tool.result.before_commit"
    TOOL_RESULT_PERSISTED = "tool.result.persisted"
    STEP_CONFIRM_BEFORE_COMMIT = "step.confirm.before_commit"
    STEP_CONFIRMED = "step.confirmed"
    GRAPH_CHECKPOINT_SAVED = "graph.checkpoint.saved"
    TERMINAL_BEFORE_COMMIT = "terminal.before_commit"
    RECOVERY_SCAN_BEFORE_COMMIT = "recovery.scan.before_commit"


class InjectedCrash(BaseException):
    """Test-only process-loss signal; lifecycle handlers must let it escape."""


class FaultInjector(Protocol):
    async def checkpoint(
        self, point: FaultPoint | str, context: dict[str, Any]
    ) -> None: ...


class NoopFaultInjector:
    """Production default with no side effects."""

    async def checkpoint(
        self, point: FaultPoint | str, context: dict[str, Any]
    ) -> None:
        del point, context


NOOP_FAULT_INJECTOR = NoopFaultInjector()
