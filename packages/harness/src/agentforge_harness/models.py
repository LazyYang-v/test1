"""Model Adapter protocol, deterministic fake, and bounded JSON repair.

The harness treats model output as untrusted data. It validates the raw JSON
against a schema, gives the model a bounded number of repair turns, and fails
explicitly when the budget is exhausted. It never supplies a default
conclusion of its own.
"""

from __future__ import annotations

import asyncio
import json
import math
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from decimal import Decimal
from email.utils import parsedate_to_datetime
from typing import Any, Protocol

import httpx

from jsonschema import Draft202012Validator

from agentforge_harness.schemas import COMPOSE_SCHEMA, PLAN_SCHEMA

__all__ = [
    "DeepSeekModel",
    "FakeModel",
    "FakeScript",
    "ModelAdapter",
    "ModelAdapterRouter",
    "ModelError",
    "ModelOutputError",
    "ModelProviderConfigurationError",
    "ModelProviderError",
    "ModelProviderRejectedError",
    "ModelProviderRateLimitedError",
    "ModelProviderServerError",
    "ModelProviderTimeoutError",
    "ModelProviderUnavailableError",
    "ModelRequest",
    "ModelResponse",
    "ModelTimeoutError",
    "generate_json",
]


class ModelError(RuntimeError):
    """Base class for typed model failures."""

    code = "MODEL_ERROR"

    def __init__(self, message: str, *, details: Any = None):
        super().__init__(message)
        self.details = details


class ModelProviderConfigurationError(ModelError):
    code = "MODEL_PROVIDER_CONFIGURATION"


class ModelTimeoutError(ModelError):
    code = "MODEL_TIMEOUT"


class ModelProviderError(ModelError):
    """A provider-side failure with sanitized request metadata."""

    code = "MODEL_PROVIDER_ERROR"

    def __init__(
        self,
        message: str,
        *,
        provider_request_id: str | None = None,
        retry_after_seconds: float | None = None,
        duration_ms: int = 0,
        provider_parameters: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.provider_request_id = provider_request_id
        self.retry_after_seconds = retry_after_seconds
        self.duration_ms = duration_ms
        self.provider_parameters = provider_parameters or {}


class ModelProviderRejectedError(ModelProviderError):
    """Non-retryable provider rejection such as invalid credentials."""

    code = "MODEL_PROVIDER_REJECTED"


class ModelProviderTimeoutError(ModelProviderError):
    code = "MODEL_PROVIDER_TIMEOUT"


class ModelProviderUnavailableError(ModelProviderError):
    code = "MODEL_PROVIDER_UNAVAILABLE"


class ModelProviderRateLimitedError(ModelProviderError):
    code = "MODEL_PROVIDER_RATE_LIMITED"


class ModelProviderServerError(ModelProviderError):
    code = "MODEL_PROVIDER_5XX"


class ModelOutputError(ModelError):
    code = "MODEL_OUTPUT_INVALID"


@dataclass(frozen=True)
class ModelRequest:
    messages: list[dict[str, Any]]
    schema: dict[str, Any]
    timeout_seconds: float
    metadata: dict[str, Any] = field(default_factory=dict)
    context_hash: str = ""
    max_output_tokens: int | None = None
    provider_parameters: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ModelResponse:
    content: Any
    input_tokens: int
    output_tokens: int
    estimated_cost: Decimal
    provider_request_id: str | None = None
    finish_reason: str = "stop"
    duration_ms: int = 0
    provider_parameters: dict[str, Any] = field(default_factory=dict)


class ModelAdapter(Protocol):
    """Minimal contract a future DeepSeek adapter must also satisfy."""

    async def generate(self, request: ModelRequest) -> ModelResponse: ...


class ModelInvocationRecorder(Protocol):
    async def record_started(self, invocation_key: str, request: ModelRequest) -> None: ...

    async def record_finished(
        self,
        invocation_key: str,
        response: ModelResponse | None,
        error: BaseException | None,
    ) -> None: ...


@dataclass(frozen=True)
class FakeScript:
    """One scripted fake-model turn."""

    content: Any
    delay_seconds: float = 0.0
    error: BaseException | None = None
    input_tokens: int = 32
    output_tokens: int = 16
    estimated_cost: Decimal = Decimal("0.000048")
    finish_reason: str = "stop"


def _estimate_tokens(value: Any) -> int:
    return max(1, len(json.dumps(value, sort_keys=True, default=str)) // 4)


class FakeModel:
    """Deterministic, scriptable adapter used by offline and integration tests.

    ``scripts`` maps a stable step key (for example ``plan_metrics:0``) to an
    ordered list of turns. When the list is exhausted, or no script exists, the
    adapter produces the canonical P0 plan/compose payload from request metadata.
    """

    provider = "fake"
    model_name = "fake-v1"

    def __init__(
        self,
        scripts: dict[str, list[FakeScript]] | None = None,
        *,
        barrier: asyncio.Event | None = None,
    ) -> None:
        self._scripts = {key: list(value) for key, value in (scripts or {}).items()}
        self._barrier = barrier
        self.requests: list[ModelRequest] = []

    def _next_script(self, step_key: str) -> FakeScript | None:
        turns = self._scripts.get(step_key)
        if turns:
            return turns.pop(0)
        return None

    @staticmethod
    def _default_content(request: ModelRequest) -> dict[str, Any]:
        step_key = str(request.metadata.get("step_key", ""))
        if step_key.startswith("plan_metrics"):
            task_input = request.metadata["task_input"]
            return {
                "metric": "gmv",
                "metric_version": 1,
                "dimensions": ["region_code"],
                "filters": {"region_code": list(task_input["region_codes"])},
                "time_range": dict(task_input["date_range"]),
                "comparison": task_input.get("compare_mode", "previous_period"),
                "limit": 100,
            }
        if step_key.startswith("compose_analysis"):
            return _default_compose(request.metadata.get("evidence_ids", []))
        raise ModelOutputError(
            f"fake model has no default output for step {step_key!r}"
        )

    async def generate(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        step_key = str(request.metadata.get("step_key", ""))
        script = self._next_script(step_key)
        if self._barrier is not None:
            await self._barrier.wait()
        if script is not None and script.delay_seconds:
            await asyncio.sleep(script.delay_seconds)
        if script is not None and script.error is not None:
            raise script.error
        if script is not None:
            content = script.content
            input_tokens = script.input_tokens
            output_tokens = script.output_tokens
            cost = script.estimated_cost
        else:
            content = self._default_content(request)
            input_tokens = _estimate_tokens(request.messages)
            output_tokens = _estimate_tokens(content)
            cost = Decimal(input_tokens + output_tokens) * Decimal("0.000001")
        return ModelResponse(
            content=content,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            estimated_cost=cost,
            provider_request_id=f"fake-{len(self.requests)}",
            finish_reason=("stop" if script is None else script.finish_reason),
            provider_parameters=dict(request.provider_parameters),
        )


class ModelAdapterRouter:
    """Select the configured adapter from an immutable model configuration."""

    def __init__(self, adapters: dict[str, ModelAdapter]) -> None:
        self._adapters = dict(adapters)

    def resolve(self, provider: str) -> ModelAdapter:
        try:
            return self._adapters[provider]
        except KeyError as error:
            raise ModelProviderConfigurationError(
                f"model provider {provider!r} is not configured"
            ) from error


class DeepSeekModel:
    """DeepSeek Chat Completions adapter using the shared ModelAdapter contract."""

    provider = "deepseek"

    _RESERVED_PARAMETERS = frozenset(
        {"model", "messages", "stream", "response_format", "max_tokens"}
    )

    def __init__(
        self,
        *,
        model_name: str,
        api_key: str,
        base_url: str = "https://api.deepseek.com/v1",
        client: httpx.AsyncClient | None = None,
        input_cost_per_million: Decimal = Decimal("0"),
        output_cost_per_million: Decimal = Decimal("0"),
    ) -> None:
        if not api_key.strip():
            raise ModelProviderConfigurationError("DeepSeek API key is required")
        parsed = httpx.URL(base_url)
        if (
            parsed.scheme != "https"
            or not parsed.host
            or parsed.userinfo
            or parsed.query
            or parsed.fragment
        ):
            raise ModelProviderConfigurationError(
                "DeepSeek base URL must be a credential-free HTTPS URL"
            )
        if not model_name.strip():
            raise ModelProviderConfigurationError("DeepSeek model name is required")
        if (
            not input_cost_per_million.is_finite()
            or not output_cost_per_million.is_finite()
            or input_cost_per_million < 0
            or output_cost_per_million < 0
        ):
            raise ModelProviderConfigurationError(
                "model token costs must be finite and non-negative"
            )
        self.model_name = model_name
        self._api_key = api_key
        self._endpoint = str(parsed).rstrip("/") + "/chat/completions"
        self._client = client or httpx.AsyncClient()
        self._owns_client = client is None
        self._input_cost_per_million = input_cost_per_million
        self._output_cost_per_million = output_cost_per_million

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    @staticmethod
    def _validate_prompt(messages: list[dict[str, Any]]) -> None:
        text = "\n".join(
            str(message.get("content", ""))
            for message in messages
            if isinstance(message, dict)
        )
        lowered = text.lower()
        if "json" not in lowered or "example" not in lowered or "{" not in text:
            raise ValueError(
                'DeepSeek JSON prompts must contain "json" and a JSON example'
            )

    @staticmethod
    def _retry_after(value: str | None) -> float | None:
        if not value:
            return None
        try:
            seconds = float(value)
            return max(0.0, seconds) if math.isfinite(seconds) else None
        except ValueError:
            try:
                date = parsedate_to_datetime(value)
                if date.tzinfo is None:
                    date = date.replace(tzinfo=timezone.utc)
                return max(0.0, (date - datetime.now(timezone.utc)).total_seconds())
            except (TypeError, ValueError, OverflowError):
                return None

    async def generate(self, request: ModelRequest) -> ModelResponse:
        self._validate_prompt(request.messages)
        parameters = dict(request.provider_parameters)
        invalid = self._RESERVED_PARAMETERS.intersection(parameters)
        if invalid:
            raise ValueError(
                "provider parameters cannot override " + ", ".join(sorted(invalid))
            )
        payload = {
            "model": self.model_name,
            "messages": request.messages,
            "response_format": {"type": "json_object"},
            "stream": False,
            **parameters,
        }
        if request.max_output_tokens is not None:
            payload["max_tokens"] = request.max_output_tokens

        started = time.perf_counter()
        try:
            response = await self._client.post(
                self._endpoint,
                headers={"Authorization": f"Bearer {self._api_key}"},
                json=payload,
                timeout=request.timeout_seconds,
            )
        except httpx.TimeoutException as error:
            elapsed_ms = round((time.perf_counter() - started) * 1000)
            raise ModelProviderTimeoutError(
                "DeepSeek request timed out",
                duration_ms=elapsed_ms,
                provider_parameters={
                    "model": self.model_name,
                    "response_format": {"type": "json_object"},
                    "stream": False,
                    **parameters,
                    **({"max_tokens": request.max_output_tokens}
                       if request.max_output_tokens is not None else {}),
                }
            ) from error
        except httpx.RequestError as error:
            elapsed_ms = round((time.perf_counter() - started) * 1000)
            raise ModelProviderUnavailableError(
                "DeepSeek could not be reached",
                duration_ms=elapsed_ms,
                provider_parameters={
                    "model": self.model_name,
                    "response_format": {"type": "json_object"},
                    "stream": False,
                    **parameters,
                    **({"max_tokens": request.max_output_tokens}
                       if request.max_output_tokens is not None else {}),
                }
            ) from error

        request_id = response.headers.get("x-request-id")
        elapsed_ms = max(0, round((time.perf_counter() - started) * 1000))
        request_parameters = {
            "model": self.model_name,
            "response_format": {"type": "json_object"},
            "stream": False,
            **parameters,
            **({"max_tokens": request.max_output_tokens}
               if request.max_output_tokens is not None else {}),
        }
        if response.status_code == 429:
            raise ModelProviderRateLimitedError(
                "DeepSeek rate limit reached",
                provider_request_id=request_id,
                retry_after_seconds=self._retry_after(
                    response.headers.get("retry-after")
                ),
                duration_ms=elapsed_ms,
                provider_parameters=request_parameters,
            )
        if 500 <= response.status_code <= 599:
            raise ModelProviderServerError(
                f"DeepSeek returned HTTP {response.status_code}",
                provider_request_id=request_id,
                duration_ms=elapsed_ms,
                provider_parameters=request_parameters,
            )
        if response.is_error:
            raise ModelProviderRejectedError(
                f"DeepSeek rejected request with HTTP {response.status_code}",
                provider_request_id=request_id,
                duration_ms=elapsed_ms,
                provider_parameters=request_parameters,
            )
        try:
            data = response.json()
            choice = data["choices"][0]
            content = choice["message"]["content"]
            finish_reason = choice["finish_reason"]
            usage = data["usage"]
            input_tokens = int(usage["prompt_tokens"])
            output_tokens = int(usage["completion_tokens"])
        except (ValueError, KeyError, IndexError, TypeError) as error:
            raise ModelProviderUnavailableError(
                "DeepSeek returned an invalid completion response",
                provider_request_id=request_id,
                duration_ms=elapsed_ms,
                provider_parameters=request_parameters,
            ) from error
        if input_tokens < 0 or output_tokens < 0:
            raise ModelProviderUnavailableError(
                "DeepSeek returned invalid token usage",
                provider_request_id=request_id,
                duration_ms=elapsed_ms,
                provider_parameters=request_parameters,
            )

        cost = (
            Decimal(input_tokens) * self._input_cost_per_million
            + Decimal(output_tokens) * self._output_cost_per_million
        ) / Decimal(1_000_000)
        if request_id is None:
            provider_id = data.get("id")
            request_id = provider_id if isinstance(provider_id, str) else None
        return ModelResponse(
            content=content,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            estimated_cost=cost,
            provider_request_id=request_id,
            finish_reason=str(finish_reason),
            duration_ms=elapsed_ms,
            provider_parameters=request_parameters,
        )


def _default_compose(evidence_ids: list[str]) -> dict[str, Any]:
    ids = [str(value) for value in evidence_ids]
    return {
        "summary": (
            "CN_EAST GMV decreased in the current window; the platform-computed "
            "change is shown in the metrics section."
        ),
        "findings": [
            {
                "text": (
                    "The deterministic GMV metric confirms the reported change "
                    "for the frozen comparison window."
                ),
                "evidence_ids": ids[:1],
            }
        ],
        "hypotheses": [
            {
                "text": "Campaign timing may explain the change.",
                "status": "unverified",
            }
        ],
        "recommendations": [
            "Compare the window with the live campaign calendar.",
        ],
        "limitations": [
            "The analysis is limited to the authorized region scope and the "
            "frozen data snapshot.",
        ],
    }


def _validator(schema: dict[str, Any]) -> Draft202012Validator:
    return Draft202012Validator(schema)


def _schema_errors(schema: dict[str, Any], value: Any) -> list[str]:
    errors = sorted(
        _validator(schema).iter_errors(value),
        key=lambda error: list(error.absolute_path),
    )
    return [
        f"{'/'.join(str(part) for part in error.absolute_path) or '$'}: {error.message}"
        for error in errors
    ]


def _parse_content(response: ModelResponse) -> Any:
    if isinstance(response.content, (dict, list)):
        return response.content
    if not isinstance(response.content, str):
        raise ValueError("model content must be a JSON object or a string")
    if not response.content.strip():
        raise ValueError("model content was empty")
    return json.loads(response.content)


def _repair_request(
    request: ModelRequest, previous: ModelResponse, problem: str
) -> ModelRequest:
    raw = (
        previous.content
        if isinstance(previous.content, str)
        else json.dumps(previous.content, sort_keys=True)
    )
    messages = [
        *request.messages,
        {"role": "assistant", "content": raw},
        {
            "role": "user",
            "content": (
                "Repair the previous response: it was not valid JSON for the "
                f"required schema. Problem: {problem}. Return only a corrected "
                "json object that satisfies the schema. Do not add prose."
            ),
        },
    ]
    return replace(request, messages=messages)


async def generate_json(
    adapter: ModelAdapter,
    request: ModelRequest,
    schema: dict[str, Any],
    *,
    max_repairs: int = 2,
    recorder: ModelInvocationRecorder | None = None,
    before_call: Callable[[], Awaitable[None]] | None = None,
) -> tuple[dict[str, Any], list[ModelResponse]]:
    """Generate schema-valid JSON, repairing at most ``max_repairs`` times.

    Every physical adapter call is a distinct at-least-once model invocation. It
    is reported to the recorder before and after execution, including the failed
    attempts whose output did not validate, so cost accounting stays truthful.

    ``before_call`` is awaited before every physical call (including repair
    turns) so the Runtime can stop issuing calls after cancellation.
    """
    prefix = str(request.metadata.get("invocation_key_prefix", "model"))
    current = request
    responses: list[ModelResponse] = []
    problem = ""

    for call_index in range(max_repairs + 1):
        if before_call is not None:
            await before_call()
        invocation_key = f"{prefix}:{call_index}"
        if recorder is not None:
            await recorder.record_started(invocation_key, current)
        try:
            try:
                async with asyncio.timeout(request.timeout_seconds):
                    response = await adapter.generate(current)
            except TimeoutError as error:
                failure = ModelTimeoutError(
                    f"model call exceeded {request.timeout_seconds:.3f}s"
                )
                if recorder is not None:
                    await recorder.record_finished(invocation_key, None, failure)
                raise failure from error

            responses.append(response)
            try:
                if response.finish_reason == "length":
                    raise ModelOutputError("model output was truncated at the token limit")
                decoded = _parse_content(response)
                errors = _schema_errors(schema, decoded)
                if errors:
                    raise ModelOutputError(
                        "model JSON does not satisfy the schema", details=errors
                    )
            except (json.JSONDecodeError, ValueError, ModelOutputError) as error:
                failure = (
                    error
                    if isinstance(error, ModelOutputError)
                    else ModelOutputError(f"model output is not valid JSON: {error}")
                )
                if recorder is not None:
                    await recorder.record_finished(invocation_key, response, failure)
                problem = (
                    "; ".join(failure.details)
                    if failure.details
                    else str(failure)
                )
                if call_index == max_repairs:
                    raise ModelOutputError(
                        "model output remained invalid after bounded repairs",
                        details={
                            "attempts": max_repairs + 1,
                            "last_error": problem,
                        },
                    ) from error
                current = _repair_request(current, response, problem)
                continue

            if recorder is not None:
                await recorder.record_finished(invocation_key, response, None)
            return decoded, responses
        except ModelOutputError:
            raise
        except Exception as error:  # adapter-level typed failure
            if recorder is not None:
                await recorder.record_finished(invocation_key, None, error)
            raise

    raise ModelOutputError("model output remained invalid after bounded repairs")
