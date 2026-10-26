"""Safe structured logging, request correlation, and optional OTel settings."""

import json
import logging

import pytest
from httpx import ASGITransport, AsyncClient
from fastapi.testclient import TestClient

from agentforge_api.app import create_app
from agentforge_api.config import Settings
from agentforge_api.observability import configure_telemetry
from agentforge_core.observability import JsonFormatter, log_event


@pytest.mark.parametrize("request_id", ["bad\r\nforged", "x" * 129, " spaces "])
async def test_request_id_replaces_malicious_or_oversized_values(request_id):
    app = create_app(Settings(jwt_secret="unit-test-signing-secret-at-least-32-bytes"))
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.get(
            "/health/live", headers={"X-Request-ID": request_id}
        )

    sanitized = response.headers["X-Request-ID"]
    assert sanitized != request_id
    assert len(sanitized) <= 128
    assert sanitized.isascii()


async def test_request_id_preserves_safe_caller_correlation():
    app = create_app(Settings(jwt_secret="unit-test-signing-secret-at-least-32-bytes"))
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.get(
            "/health/live", headers={"X-Request-ID": "client-req.2026:42"}
        )
    assert response.headers["X-Request-ID"] == "client-req.2026:42"


def test_json_logs_include_safe_correlation_and_exclude_payloads(caplog):
    logger = logging.getLogger("agentforge.test.observability")
    with caplog.at_level(logging.INFO, logger=logger.name):
        log_event(
            logger,
            logging.INFO,
            "model.failed",
            trace_id="task-trace-1",
            task_id="task-1",
            attempt_id="attempt-1",
            error_code="MODEL_OUTPUT_INVALID",
            prompt="SENSITIVE PROMPT",
            password="SENSITIVE PASSWORD",
            dsn="postgresql://secret",
            token="secret-token",
        )

    encoded = JsonFormatter().format(caplog.records[-1])
    parsed = json.loads(encoded)
    assert parsed["trace_id"] == "task-trace-1"
    assert parsed["task_id"] == "task-1"
    assert parsed["attempt_id"] == "attempt-1"
    assert parsed["error_code"] == "MODEL_OUTPUT_INVALID"
    assert all(
        value not in encoded
        for value in (
            "SENSITIVE PROMPT",
            "SENSITIVE PASSWORD",
            "postgresql://secret",
            "secret-token",
        )
    )


def test_otel_disabled_api_startup_does_not_require_endpoint(monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    settings = Settings()
    assert settings.otel_enabled is False
    assert settings.otel_exporter_otlp_endpoint is None
    app = create_app(settings)
    with TestClient(app) as client:
        assert client.get("/health/live").json() == {"status": "alive"}


def test_enabling_otel_without_endpoint_fails_clearly(monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    with pytest.raises(RuntimeError, match="no OTLP endpoint"):
        configure_telemetry(True, None)
