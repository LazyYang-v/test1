"""API telemetry adapter using the shared core observability contract."""

from agentforge_core.observability import (
    configure_json_logging,
    configure_telemetry as _configure_telemetry,
    log_event,
)


def configure_telemetry(enabled: bool, endpoint: str | None) -> None:
    _configure_telemetry(enabled, endpoint, service_name="agentforge-api")


__all__ = ["configure_json_logging", "configure_telemetry", "log_event"]
