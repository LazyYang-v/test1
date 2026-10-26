"""Frozen error classification table for the P0 retry policy.

Retryable errors leave the Task in ``retry_wait`` with a jittered backoff.
Everything else terminates the Task as ``failed`` (or is handled as an
ownership loss / cancellation outside this table). Unknown codes fail closed.
"""

from __future__ import annotations

from agentforge_harness.gateway import ToolError
from agentforge_harness.models import ModelError
from agentforge_harness.report import ReportValidationError
from agentforge_runtime.errors import LeaseLostError, TaskNotRunningError

RETRYABLE_ERROR_CODES: frozenset[str] = frozenset(
    {
        # Provider timeout / unavailable / 5xx / rate limit.
        "MODEL_TIMEOUT",
        "MODEL_PROVIDER_TIMEOUT",
        "MODEL_PROVIDER_UNAVAILABLE",
        "MODEL_PROVIDER_5XX",
        "MODEL_PROVIDER_RATE_LIMITED",
        # A read-only tool can be safely retried after a timeout.
        "TOOL_TIMEOUT",
    }
)

NON_RETRYABLE_ERROR_CODES: frozenset[str] = frozenset(
    {
        # Bounded JSON repair exhausted or the plan is still invalid.
        "MODEL_OUTPUT_INVALID",
        # Tool authorization, binding, schema, and budget failures.
        "REGION_ACCESS_DENIED",
        "TOOL_NOT_BOUND",
        "TOOL_INPUT_INVALID",
        "TOOL_OUTPUT_INVALID",
        "BUDGET_EXCEEDED",
        "TOOL_UNAVAILABLE",
        "TOOL_NOT_REGISTERED",
        "TOOL_POLICY_INVALID",
        # Internal invariant violations and unsupported graphs.
        "INTERNAL_ERROR",
        "GRAPH_UNSUPPORTED",
        # Deadline is terminal; a new attempt must not be scheduled.
        "DEADLINE_EXCEEDED",
        "TOOL_EXECUTION_FAILED",
    }
)

NON_RETRYABLE_ERROR_PREFIXES: tuple[str, ...] = ("REPORT_",)


def classify_error_code(error: BaseException) -> str:
    """Return the frozen error code for an exception, defaulting closed."""
    code = getattr(error, "code", None)
    if isinstance(code, str) and code:
        return code
    # Typed harness/runtime errors always expose ``code``; a plain exception is
    # an internal invariant violation and must never be retried.
    if isinstance(error, (ModelError, ReportValidationError, ToolError,
                          TaskNotRunningError, LeaseLostError)):
        fallback = getattr(type(error), "code", None)
        if isinstance(fallback, str) and fallback:
            return fallback
    return "INTERNAL_ERROR"


def is_retryable(code: str) -> bool:
    """Only the frozen retryable table is eligible for backoff.

    ``NON_RETRYABLE_ERROR_CODES``/``NON_RETRYABLE_ERROR_PREFIXES`` are the
    explicit terminal side of the table; every code outside either side also
    fails closed.
    """
    return code in RETRYABLE_ERROR_CODES
