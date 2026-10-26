"""The runtime recorder persists model calls through its SQL interface."""

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agentforge_harness.models import ModelResponse, ModelRequest
from agentforge_runtime.contracts import AttemptLease
from agentforge_worker.graph import RuntimeModelRecorder


class _Connection:
    def __init__(self):
        self.execute = AsyncMock()

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _Pool:
    def __init__(self):
        self.connection = _Connection()
        self.acquire = lambda: self.connection


@pytest.mark.parametrize("failed", [False, True])
async def test_invocation_record_keeps_provider_metadata_and_timing(failed):
    pool = _Pool()
    recorder = RuntimeModelRecorder(
        pool=pool,
        lease=AttemptLease(
            attempt_id="attempt-1",
            task_id="task-1",
            attempt_no=1,
            worker_id="worker-unit",
            lease_token="lease-token",
            fencing_epoch=1,
            trace_id="trace-1",
        ),
        agent_version_id="agent-1",
        provider="deepseek",
        model_name="deepseek-chat",
    )
    request = ModelRequest(
        messages=[{"role": "system", "content": "Return json. Example: {}"}],
        schema={},
        timeout_seconds=1,
        context_hash="a" * 64,
        provider_parameters={"temperature": 0, "top_p": 0.8},
    )
    response = ModelResponse(
        content="{}",
        input_tokens=15,
        output_tokens=7,
        estimated_cost=Decimal("0.00001"),
        provider_request_id="req-mocked-5",
        duration_ms=42,
        provider_parameters={
            "model": "deepseek-chat",
            "response_format": {"type": "json_object"},
            "temperature": 0,
            "top_p": 0.8,
            "max_tokens": 128,
        },
    )

    await recorder.record_started("step:model:0", request)
    await recorder.record_finished(
        "step:model:0",
        response if not failed else None,
        SimpleNamespace(
            code="MODEL_PROVIDER_5XX",
            provider_request_id="req-mocked-5",
            duration_ms=42,
            provider_parameters=response.provider_parameters,
        )
        if failed
        else None,
    )

    started = pool.connection.execute.await_args_list[0]
    finished = pool.connection.execute.await_args_list[1]
    assert "provider_parameters" in started.args[0]
    assert "trace_id" in started.args[0]
    assert '"temperature":0' in started.args[-2]
    assert started.args[-1] == "trace-1"
    assert finished.args[0].count("$11") == 1 and finished.args[0].count("$12") == 1
    assert finished.args[1:4] == ("task-1", "step:model:0", "attempt-1")
    assert finished.args[4] == ("failed" if failed else "succeeded")
    assert finished.args[5] == (0 if failed else 15)
    assert finished.args[6] == (0 if failed else 7)
    assert finished.args[7] == (Decimal("0") if failed else Decimal("0.00001"))
    assert finished.args[8] == ("MODEL_PROVIDER_5XX" if failed else None)
    assert finished.args[9] == "req-mocked-5"
    assert finished.args[10] == 42
    assert '"max_tokens":128' in finished.args[11]
    assert finished.args[12] == "trace-1"
