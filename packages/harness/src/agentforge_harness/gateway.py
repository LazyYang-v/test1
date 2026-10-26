"""Nine-step Tool Gateway skeleton (HS-03/HS-04, design baseline section 7).

Steps: resolve published tool version, validate input schema, check live user
and region grants, verify Agent binding and budget, validate lease/cancellation,
reuse a stable ToolInvocation, dispatch only to registered read-only executors,
validate output schema, then persist acceptance and audit/trace.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping, Protocol, runtime_checkable

from jsonschema import Draft202012Validator

__all__ = [
    "ToolContext",
    "ToolDescriptor",
    "ToolError",
    "ToolExecutor",
    "ToolGateway",
    "ToolGatewayBackend",
    "ToolInputError",
    "ToolOutputError",
    "ToolPolicyError",
    "compute_invocation_key",
    "merge_policy",
    "validate_schema",
]


class ToolError(RuntimeError):
    """Typed tool failure that terminates the current attempt."""

    def __init__(self, code: str, message: str, *, details: Any = None):
        self.code = code
        self.details = details
        super().__init__(f"{code}: {message}")


class ToolInputError(ToolError):
    pass


class ToolOutputError(ToolError):
    pass


class ToolPolicyError(ToolError):
    def __init__(self, message: str, *, details: Any = None):
        super().__init__("TOOL_POLICY_INVALID", message, details=details)


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def compute_invocation_key(step_key: str, request: Any) -> str:
    """Stable across attempts and processes for the same logical request."""
    digest = hashlib.sha256(_canonical(request).encode("utf-8")).hexdigest()
    return f"{step_key}:{digest}"


def _json_equal(left: Any, right: Any) -> bool:
    if type(left) in (int, float) and type(right) in (int, float):
        return left == right
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(
            _json_equal(value, right[key]) for key, value in left.items()
        )
    if isinstance(left, list):
        return len(left) == len(right) and all(
            _json_equal(value, other) for value, other in zip(left, right)
        )
    return left == right


def merge_policy(
    base_policy: Mapping[str, Any], policy_override: Mapping[str, Any]
) -> dict[str, Any]:
    """Merge a binding override that may only tighten the base policy."""
    if not isinstance(base_policy, Mapping) or not isinstance(policy_override, Mapping):
        raise ToolPolicyError("policy and override must be objects")
    merged = dict(base_policy)
    for key, value in policy_override.items():
        if key not in base_policy:
            raise ToolPolicyError(f"override key {key!r} is not in the base policy")
        base_value = base_policy[key]
        if key.startswith("max_"):
            if (
                isinstance(base_value, bool)
                or isinstance(value, bool)
                or not isinstance(base_value, (int, float))
                or not isinstance(value, (int, float))
                or value > base_value
            ):
                raise ToolPolicyError(f"{key} may only decrease")
            merged[key] = value
        elif key.startswith("allowed_"):
            if (
                not isinstance(base_value, list)
                or not isinstance(value, list)
                or any(
                    not any(_json_equal(candidate, permitted) for permitted in base_value)
                    for candidate in value
                )
            ):
                raise ToolPolicyError(f"{key} may only narrow the allowed set")
            merged[key] = value
        elif key.startswith("require_"):
            if base_value is not True or value is not True:
                raise ToolPolicyError(f"{key} may only be required")
            merged[key] = True
        elif key.startswith("allow_"):
            if (
                not isinstance(base_value, bool)
                or not isinstance(value, bool)
                or (base_value is False and value is not False)
            ):
                raise ToolPolicyError(f"{key} may only disable")
            merged[key] = value
        else:
            raise ToolPolicyError(f"override key {key!r} is not a recognized policy")
    return merged


def validate_schema(value: Any, schema: dict[str, Any], *, error_code: str) -> None:
    errors = [
        f"{'/'.join(str(part) for part in error.absolute_path) or '$'}: {error.message}"
        for error in sorted(
            Draft202012Validator(schema).iter_errors(value),
            key=lambda error: list(error.absolute_path),
        )
    ]
    if errors:
        error_type = ToolInputError if error_code.endswith("INPUT_INVALID") else ToolOutputError
        raise error_type(error_code, "value does not satisfy the tool schema", details=errors)


@dataclass(frozen=True)
class ToolContext:
    task_id: str
    attempt_id: str
    agent_version_id: str
    principal_id: str
    region_scope: tuple[str, ...]
    lease_token: str
    fencing_epoch: int
    trace_id: str | None = None
    step_key: str = "execute_metrics:0"
    task_snapshot_id: str = ""


@dataclass(frozen=True)
class ToolDescriptor:
    id: str
    tool_key: str
    version: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    policy_json: dict[str, Any]
    execution_kind: str
    is_read_only: bool
    timeout_ms: int
    status: str


@runtime_checkable
class ToolExecutor(Protocol):
    tool_key: str
    read_only: bool

    async def execute(self, request: dict[str, Any], context: ToolContext) -> dict[str, Any]: ...


class ToolGatewayBackend(Protocol):
    async def resolve_tool(self, tool_key: str) -> ToolDescriptor | None: ...

    async def binding_override(
        self, agent_version_id: str, tool_version_id: str
    ) -> dict[str, Any] | None: ...

    async def has_live_region_grant(
        self, principal_id: str, region_code: str
    ) -> bool: ...

    async def lease_is_valid(self, context: ToolContext) -> bool: ...

    async def accepted_call_count(self, task_id: str, tool_version_id: str) -> int: ...

    async def find_invocation(
        self, task_id: str, invocation_key: str
    ) -> dict[str, Any] | None: ...

    async def reject(
        self,
        context: ToolContext,
        descriptor: ToolDescriptor | None,
        invocation_key: str,
        request_hash: str,
        request_json: dict[str, Any],
        code: str,
        detail: str,
    ) -> None: ...

    async def accept(
        self,
        context: ToolContext,
        descriptor: ToolDescriptor,
        invocation_key: str,
        request_hash: str,
        request_json: dict[str, Any],
        result: dict[str, Any],
    ) -> dict[str, Any]: ...


class ToolGateway:
    """Orchestrates the nine checks around a registered read-only executor."""

    def __init__(
        self,
        backend: ToolGatewayBackend,
        executors: Mapping[str, ToolExecutor],
    ) -> None:
        self._backend = backend
        self._executors = dict(executors)

    async def invoke(
        self,
        context: ToolContext,
        tool_key: str,
        request: dict[str, Any],
    ) -> dict[str, Any]:
        request_hash = hashlib.sha256(_canonical(request).encode("utf-8")).hexdigest()
        invocation_key = compute_invocation_key(context.step_key, request)

        async def reject(
            code: str,
            detail: str,
            descriptor: ToolDescriptor | None = None,
        ) -> None:
            await self._backend.reject(
                context,
                descriptor,
                invocation_key,
                request_hash,
                request,
                code,
                detail,
            )
            raise ToolError(code, detail)

        # 1. Verify the published tool version.
        descriptor = await self._backend.resolve_tool(tool_key)
        if descriptor is None or descriptor.status != "active":
            await reject("TOOL_UNAVAILABLE", "tool version is not active")
        assert descriptor is not None
        if descriptor.execution_kind != "internal" or not descriptor.is_read_only:
            await reject(
                "TOOL_NOT_REGISTERED",
                "only registered read-only internal tools may execute",
                descriptor,
            )

        # 2. Validate input schema.
        try:
            validate_schema(
                request, descriptor.input_schema, error_code="TOOL_INPUT_INVALID"
            )
        except ToolInputError as error:
            await reject(error.code, str(error), descriptor)

        # 3. Live user region authorization, never the stale snapshot.
        requested_regions = list(
            (request.get("filters") or {}).get("region_code") or []
        )
        if (
            not context.region_scope
            or not requested_regions
            or any(region not in context.region_scope for region in requested_regions)
        ):
            await reject(
                "REGION_ACCESS_DENIED",
                "requested regions are outside the task region scope",
                descriptor,
            )
        for region in requested_regions:
            if not await self._backend.has_live_region_grant(
                context.principal_id, region
            ):
                await reject(
                    "REGION_ACCESS_DENIED",
                    f"live grant for {region} is missing",
                    descriptor,
                )

        # 4. Agent binding and tightened budget.
        override = await self._backend.binding_override(
            context.agent_version_id, descriptor.id
        )
        if override is None:
            await reject(
                "TOOL_NOT_BOUND",
                "tool version is not bound to this Agent version",
                descriptor,
            )
        try:
            policy = merge_policy(descriptor.policy_json, override)
        except ToolPolicyError as error:
            await reject(error.code, str(error), descriptor)
        assert isinstance(override, dict)
        previous = await self._backend.find_invocation(context.task_id, invocation_key)
        allowed_metrics = policy.get("allowed_metrics")
        if allowed_metrics is not None and request.get("metric") not in allowed_metrics:
            await reject(
                "TOOL_INPUT_INVALID",
                "metric is outside the tightened policy",
                descriptor,
            )
        max_calls = policy.get("max_calls")
        if (
            isinstance(max_calls, int)
            and not isinstance(max_calls, bool)
            and not (previous and previous.get("status") == "succeeded")
        ):
            used = await self._backend.accepted_call_count(
                context.task_id, descriptor.id
            )
            if used >= max_calls:
                await reject("BUDGET_EXCEEDED", "tool call budget is exhausted", descriptor)

        # 5. Lease and cancellation state, checked before any work and again at acceptance.
        if not await self._backend.lease_is_valid(context):
            await reject("LEASE_LOST", "the attempt lease is no longer valid", descriptor)

        # 6. Stable ToolInvocation reuse (read-only retries are safe).
        if previous is not None:
            if previous.get("request_hash") != request_hash:
                await reject(
                    "TOOL_INPUT_INVALID",
                    "invocation key was used for a different request",
                    descriptor,
                )
            if previous.get("status") == "succeeded" and previous.get("result_json"):
                return {
                    "result": previous["result_json"],
                    "evidence_id": previous.get("evidence_id"),
                    "reused": True,
                }

        # 7. Dispatch only to the registered read-only executor for this tool.
        executor = self._executors.get(tool_key)
        if executor is None or not executor.read_only:
            await reject(
                "TOOL_NOT_REGISTERED", "no read-only executor is registered", descriptor
            )
        assert executor is not None
        try:
            async with asyncio.timeout(descriptor.timeout_ms / 1000):
                result = await executor.execute(request, context)
        except TimeoutError as error:
            await reject("TOOL_TIMEOUT", "tool execution timed out", descriptor)
            raise AssertionError("unreachable") from error
        except ToolError as error:
            await reject(error.code, str(error), descriptor)
            raise AssertionError("unreachable") from error
        except Exception as error:  # executor failure is typed and closed
            await reject(
                "TOOL_EXECUTION_FAILED", "registered tool executor failed", descriptor
            )
            raise AssertionError("unreachable") from error

        # 8. Validate output schema.
        try:
            validate_schema(
                result, descriptor.output_schema, error_code="TOOL_OUTPUT_INVALID"
            )
        except ToolOutputError as error:
            await reject(error.code, str(error), descriptor)

        # 9. Persist acceptance plus audit/trace in a fenced short transaction.
        accepted = await self._backend.accept(
            context,
            descriptor,
            invocation_key,
            request_hash,
            request,
            result,
        )
        if accepted is None:
            await reject("LEASE_LOST", "lease was lost before result acceptance", descriptor)
        return accepted
