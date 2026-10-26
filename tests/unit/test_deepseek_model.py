"""Deterministic contract tests for the DeepSeek Chat Completions adapter."""

import json
from decimal import Decimal

import httpx
import pytest

from agentforge_harness.models import (
    DeepSeekModel,
    ModelProviderRateLimitedError,
    ModelProviderRejectedError,
    ModelProviderServerError,
    ModelProviderUnavailableError,
    ModelRequest,
)
from agentforge_harness.schemas import PLAN_SCHEMA


PLAN = {
    "metric": "gmv",
    "metric_version": 1,
    "dimensions": ["region_code"],
    "filters": {"region_code": ["CN_EAST"]},
    "time_range": {
        "start": "2025-03-31",
        "end_exclusive": "2025-04-07",
        "timezone": "UTC",
    },
    "comparison": "previous_period",
    "limit": 100,
}


def _request(*, messages=None, parameters=None, max_output_tokens=128):
    return ModelRequest(
        messages=messages
        or [
            {
                "role": "system",
                "content": (
                    'Return only json. Example JSON: {"metric":"gmv"}.'
                ),
            },
            {"role": "user", "content": "Plan the GMV query."},
        ],
        schema=PLAN_SCHEMA,
        timeout_seconds=1.0,
        max_output_tokens=max_output_tokens,
        provider_parameters=parameters or {},
    )


def _adapter(handler, **kwargs):
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    return DeepSeekModel(
        model_name="deepseek-chat",
        api_key="test-secret",
        client=client,
        **kwargs,
    ), client


async def test_chat_completion_uses_json_object_and_returns_provider_accounting():
    observed = {}

    def handler(request):
        observed["url"] = str(request.url)
        observed["authorization"] = request.headers["authorization"]
        observed["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            headers={"X-Request-ID": "req-live-mocked-17"},
            json={
                "choices": [
                    {
                        "message": {"content": json.dumps(PLAN)},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 41, "completion_tokens": 23},
            },
        )

    model, client = _adapter(
        handler,
        base_url="https://api.deepseek.test/v1/",
        input_cost_per_million=Decimal("0.27"),
        output_cost_per_million=Decimal("1.10"),
    )
    try:
        response = await model.generate(
            _request(parameters={"temperature": 0, "top_p": 0.8})
        )
    finally:
        await client.aclose()

    assert observed["url"] == "https://api.deepseek.test/v1/chat/completions"
    assert observed["authorization"] == "Bearer test-secret"
    assert observed["body"]["model"] == "deepseek-chat"
    assert observed["body"]["response_format"] == {"type": "json_object"}
    assert observed["body"]["max_tokens"] == 128
    assert observed["body"]["temperature"] == 0
    assert observed["body"]["top_p"] == 0.8
    assert response.content == json.dumps(PLAN)
    assert response.provider_request_id == "req-live-mocked-17"
    assert response.input_tokens == 41
    assert response.output_tokens == 23
    assert response.finish_reason == "stop"
    assert response.estimated_cost == Decimal("0.00003637")
    assert response.provider_parameters["response_format"] == {"type": "json_object"}


@pytest.mark.parametrize(
    "messages",
    [
        [{"role": "system", "content": "Return a structured object."}],
        [{"role": "system", "content": "Return json matching the schema."}],
    ],
)
async def test_prompt_must_explain_json_and_include_an_example_before_http(messages):
    def unexpected_request(request):
        pytest.fail("invalid prompt must be rejected before an HTTP request")

    model, client = _adapter(unexpected_request)
    try:
        with pytest.raises(ValueError, match="json.*example|example.*json"):
            await model.generate(_request(messages=messages))
    finally:
        await client.aclose()


@pytest.mark.parametrize(
    "status, expected_error",
    [
        (429, ModelProviderRateLimitedError),
        (503, ModelProviderServerError),
        (401, ModelProviderRejectedError),
    ],
)
async def test_http_statuses_have_stable_provider_error_classes(status, expected_error):
    def handler(request):
        headers = {"Retry-After": "7", "X-Request-ID": "req-error-2"}
        return httpx.Response(status, headers=headers, json={"error": "redacted"})

    model, client = _adapter(handler)
    try:
        with pytest.raises(expected_error) as raised:
            await model.generate(_request())
    finally:
        await client.aclose()

    assert raised.value.duration_ms >= 0
    assert raised.value.provider_parameters["response_format"] == {"type": "json_object"}
    assert raised.value.provider_parameters["model"] == "deepseek-chat"
    if status == 429:
        assert raised.value.retry_after_seconds == 7
    assert raised.value.provider_request_id == "req-error-2"
    assert "test-secret" not in str(raised.value)


async def test_network_failure_is_retryable_and_does_not_expose_credentials():
    def handler(request):
        raise httpx.ConnectError("offline", request=request)

    model, client = _adapter(handler)
    try:
        with pytest.raises(ModelProviderUnavailableError) as raised:
            await model.generate(_request())
    finally:
        await client.aclose()

    assert "test-secret" not in str(raised.value)
