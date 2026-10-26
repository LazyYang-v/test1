"""Frozen error classification table for retry versus terminal failure."""

from __future__ import annotations

import pytest

from agentforge_harness.gateway import ToolInputError, ToolOutputError
from agentforge_harness.models import (
    ModelOutputError,
    ModelProviderRateLimitedError,
    ModelProviderRejectedError,
    ModelProviderServerError,
    ModelProviderTimeoutError,
    ModelProviderUnavailableError,
    ModelTimeoutError,
)
from agentforge_harness.report import ReportValidationError
from agentforge_runtime.errors import LeaseLostError, TaskNotRunningError
from agentforge_worker.config import WorkerSettings
from agentforge_worker.errors import classify_error_code, is_retryable
from agentforge_worker.worker import Worker
from agentforge_worker.graph import UnsupportedGraphError


@pytest.mark.parametrize(
    "error",
    [
        ModelTimeoutError("timed out"),
        ModelProviderTimeoutError("provider timed out"),
        ModelProviderUnavailableError("provider unavailable"),
        ModelProviderRateLimitedError("rate limited"),
        ModelProviderServerError("provider 500"),
        # Tool timeout is raised by the gateway with a stable code.
        ToolOutputError("TOOL_TIMEOUT", "tool timed out"),
    ],
)
def test_transient_provider_and_tool_timeouts_are_retryable(error):
    assert is_retryable(classify_error_code(error)) is True


@pytest.mark.parametrize(
    "error, expected_code",
    [
        (ModelOutputError("invalid"), "MODEL_OUTPUT_INVALID"),
        (ModelProviderRejectedError("bad credentials"), "MODEL_PROVIDER_REJECTED"),
        (ReportValidationError("REPORT_EVIDENCE_MISSING", "missing"), "REPORT_EVIDENCE_MISSING"),
        (ReportValidationError("REPORT_EVIDENCE_FOREIGN", "foreign"), "REPORT_EVIDENCE_FOREIGN"),
        (ToolInputError("TOOL_INPUT_INVALID", "bad input"), "TOOL_INPUT_INVALID"),
        (ToolInputError("TOOL_NOT_BOUND", "not bound"), "TOOL_NOT_BOUND"),
        (ToolInputError("REGION_ACCESS_DENIED", "denied"), "REGION_ACCESS_DENIED"),
        (ToolInputError("BUDGET_EXCEEDED", "budget"), "BUDGET_EXCEEDED"),
        (UnsupportedGraphError("graph"), "GRAPH_UNSUPPORTED"),
        (TaskNotRunningError("DEADLINE_EXCEEDED", "deadline"), "DEADLINE_EXCEEDED"),
        (ValueError("bug"), "INTERNAL_ERROR"),
    ],
)
def test_non_retryable_errors_keep_their_code(error, expected_code):
    code = classify_error_code(error)
    assert code == expected_code
    assert is_retryable(code) is False


def test_unknown_typed_code_is_treated_as_a_terminal_invariant_violation():
    class OddError(RuntimeError):
        code = "SOMETHING_UNEXPECTED"

    code = classify_error_code(OddError("odd"))
    assert code == "SOMETHING_UNEXPECTED"
    assert is_retryable(code) is False


@pytest.mark.parametrize(
    "retry_after, expected",
    [(0, 30), (7, 30), ("9", 30), (float("nan"), 30), (45, 45)],
)
def test_retry_after_never_shortens_backoff_and_honors_finite_provider_delay(
    retry_after, expected
):
    worker = object.__new__(Worker)
    worker.settings = WorkerSettings(
        worker_db_dsn="postgresql://agentforge_worker@localhost/agentforge",
        retry_base_delay_seconds=30,
        retry_jitter_seconds=0,
    )
    error = RuntimeError("rate limit")
    error.retry_after_seconds = retry_after

    assert worker._retry_delay(error) == expected



def test_lease_loss_is_ownership_failure_not_a_retry_classification():
    code = classify_error_code(LeaseLostError("stale"))
    assert code == "LEASE_LOST"
    assert is_retryable(code) is False
