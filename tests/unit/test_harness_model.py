"""Scriptable fake model: schema validation, bounded repair, timeout, accounting."""

from decimal import Decimal

import pytest

from agentforge_harness.models import (
    FakeModel,
    FakeScript,
    ModelOutputError,
    ModelRequest,
    ModelTimeoutError,
    generate_json,
)
from agentforge_harness.schemas import PLAN_SCHEMA

TASK_INPUT = {
    "question": "Explain the change in CN_EAST GMV",
    "region_codes": ["CN_EAST"],
    "date_range": {
        "start": "2025-03-31",
        "end_exclusive": "2025-04-07",
        "timezone": "UTC",
    },
    "compare_mode": "previous_period",
}


def _request(step_key="plan_metrics:0", *, timeout=1.0) -> ModelRequest:
    return ModelRequest(
        messages=[{"role": "user", "content": "return json plan"}],
        schema=PLAN_SCHEMA,
        timeout_seconds=timeout,
        metadata={
            "step_key": step_key,
            "invocation_key_prefix": f"{step_key}:model:1",
            "task_input": TASK_INPUT,
            "evidence_ids": [],
        },
        context_hash="a" * 64,
    )


class _Recorder:
    def __init__(self):
        self.started = []
        self.finished = []

    async def record_started(self, invocation_key, request):
        self.started.append((invocation_key, request.context_hash))

    async def record_finished(self, invocation_key, response, error):
        self.finished.append(
            (
                invocation_key,
                None if response is None else response.input_tokens,
                None if response is None else response.output_tokens,
                None if response is None else response.estimated_cost,
                None if error is None else type(error).__name__,
            )
        )


async def test_default_fake_produces_a_valid_plan_with_accounting():
    recorder = _Recorder()
    content, responses = await generate_json(
        FakeModel(), _request(), PLAN_SCHEMA, recorder=recorder
    )
    assert content["metric"] == "gmv"
    assert content["filters"] == {"region_code": ["CN_EAST"]}
    assert content["time_range"] == TASK_INPUT["date_range"]
    assert len(responses) == 1
    assert responses[0].input_tokens > 0
    assert responses[0].output_tokens > 0
    assert responses[0].estimated_cost > Decimal("0")
    assert recorder.started == [("plan_metrics:0:model:1:0", "a" * 64)]
    assert recorder.finished[0][1:4] == (
        responses[0].input_tokens,
        responses[0].output_tokens,
        responses[0].estimated_cost,
    )


async def test_invalid_json_is_repaired_within_the_bound():
    model = FakeModel(
        scripts={
            "plan_metrics:0": [
                FakeScript(content="not-json"),
                FakeScript(
                    content={
                        "metric": "gmv",
                        "metric_version": 1,
                        "dimensions": ["region_code"],
                        "filters": {"region_code": ["CN_EAST"]},
                        "time_range": TASK_INPUT["date_range"],
                        "comparison": "previous_period",
                        "limit": 100,
                    }
                ),
            ]
        }
    )
    content, responses = await generate_json(model, _request(), PLAN_SCHEMA, max_repairs=2)
    assert content["metric"] == "gmv"
    assert len(responses) == 2
    assert len(model.requests) == 2
    assert any("repair" in message["content"].lower()
               for message in model.requests[1].messages)


async def test_schema_invalid_output_is_repaired():
    model = FakeModel(
        scripts={
            "plan_metrics:0": [
                FakeScript(content={"metric": "not-registered"}),
                FakeScript(
                    content={
                        "metric": "gmv",
                        "metric_version": 1,
                        "dimensions": ["region_code"],
                        "filters": {"region_code": ["CN_EAST"]},
                        "time_range": TASK_INPUT["date_range"],
                        "comparison": "previous_period",
                        "limit": 100,
                    }
                ),
            ]
        }
    )
    content, _ = await generate_json(model, _request(), PLAN_SCHEMA, max_repairs=1)
    assert content["metric"] == "gmv"


async def test_invalid_json_exhaustion_fails_explicitly():
    model = FakeModel(
        scripts={
            "plan_metrics:0": [
                FakeScript(content="{broken"),
                FakeScript(content="{still broken"),
                FakeScript(content="{}"),
            ]
        }
    )
    with pytest.raises(ModelOutputError) as raised:
        await generate_json(model, _request(), PLAN_SCHEMA, max_repairs=2)
    assert raised.value.code == "MODEL_OUTPUT_INVALID"
    assert len(model.requests) == 3


async def test_adapter_timeout_is_a_typed_error():
    model = FakeModel(
        scripts={
            "plan_metrics:0": [FakeScript(content={}, delay_seconds=0.2)]
        }
    )
    with pytest.raises(ModelTimeoutError) as raised:
        await generate_json(
            model, _request(timeout=0.02), PLAN_SCHEMA, max_repairs=0
        )
    assert raised.value.code == "MODEL_TIMEOUT"


async def test_recorder_captures_terminal_failure():
    recorder = _Recorder()
    model = FakeModel(
        scripts={"plan_metrics:0": [FakeScript(content="{broken")]}
    )
    with pytest.raises(ModelOutputError):
        await generate_json(
            model, _request(), PLAN_SCHEMA, max_repairs=0, recorder=recorder
        )
    assert recorder.finished[0][4] == "ModelOutputError"
