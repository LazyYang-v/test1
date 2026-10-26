"""Structured, metadata-only logging and optional OpenTelemetry setup."""

from __future__ import annotations

import json
import logging
import os
import re
from contextlib import contextmanager, nullcontext
from datetime import UTC, datetime
from typing import Any

_EVENT_NAME = re.compile(r"^[a-z0-9_.-]{1,80}$")
_LOG_FIELDS = frozenset(
    {
        "trace_id",
        "task_id",
        "attempt_id",
        "worker_id",
        "error_code",
        "status_code",
        "method",
        "route",
        "request_id",
    }
)


class JsonFormatter(logging.Formatter):
    """Emit an allowlisted set of scalar fields, never the free-form message."""

    def format(self, record: logging.LogRecord) -> str:
        event = getattr(record, "event_name", "log")
        if not isinstance(event, str) or _EVENT_NAME.fullmatch(event) is None:
            event = "log"
        entry: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname.lower(),
            "logger": record.name,
            "event": event,
        }
        for field in _LOG_FIELDS:
            value = getattr(record, field, None)
            if isinstance(value, (str, int, float)) and not isinstance(value, bool):
                entry[field] = value[:160] if isinstance(value, str) else value
        return json.dumps(entry, separators=(",", ":"), ensure_ascii=True)


def log_event(
    logger: logging.Logger,
    level: int,
    event: str,
    **fields: str | int | float | None,
) -> None:
    """Log only known correlation fields and fixed event names."""
    safe_event = event if _EVENT_NAME.fullmatch(event) else "log"
    safe_fields = {
        key: value
        for key, value in fields.items()
        if key in _LOG_FIELDS
        and isinstance(value, (str, int, float))
        and not isinstance(value, bool)
    }
    logger.log(level, "", extra={"event_name": safe_event, **safe_fields})


def configure_json_logging() -> None:
    root = logging.getLogger()
    for handler in root.handlers:
        root.removeHandler(handler)
        handler.close()
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        service_logger = logging.getLogger(name)
        service_logger.handlers.clear()
        service_logger.propagate = True


def get_tracer(name: str):
    try:
        from opentelemetry import trace
    except ImportError:  # pragma: no cover - package dependency guard
        return None
    return trace.get_tracer(name)


@contextmanager
def span(name: str, **attributes: str | int | float | bool | None):
    """Create a span when the OTel API is installed; otherwise be a no-op."""
    tracer = get_tracer("agentforge")
    context = nullcontext() if tracer is None else tracer.start_as_current_span(name)
    with context as current:
        if current is not None and hasattr(current, "set_attribute"):
            for key, value in attributes.items():
                if value is not None:
                    current.set_attribute(key, value)
        yield current


def configure_telemetry(
    enabled: bool, endpoint: str | None, *, service_name: str
) -> None:
    """Set up OTLP tracing only when requested; absent endpoint fails clearly."""
    if not enabled:
        return
    resolved_endpoint = endpoint or os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    if not resolved_endpoint:
        raise RuntimeError(
            "OpenTelemetry is enabled but no OTLP endpoint is configured"
        )

    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError as error:  # pragma: no cover - packaging guard
        raise RuntimeError(
            "OpenTelemetry is enabled but the OTLP SDK/exporter is unavailable"
        ) from error

    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=resolved_endpoint))
    )
    trace.set_tracer_provider(provider)
