"""Environment-driven worker settings."""

from __future__ import annotations

import os
import socket
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings


def _default_worker_id() -> str:
    return f"worker-{socket.gethostname()}-{os.getpid()}"


class WorkerSettings(BaseSettings):
    """The worker must connect as ``agentforge_worker`` and never as a superuser."""

    worker_db_dsn: str = "postgresql://agentforge_worker@localhost:5432/agentforge"
    # The business query path never uses the worker role. It connects through a
    # separate role that can only SELECT the authorized metric view.
    business_ro_db_dsn: str = (
        "postgresql://agentforge_business_ro@localhost:5432/agentforge"
    )
    business_ro_statement_timeout_ms: int = Field(default=3000, ge=100, le=15000)
    worker_id: str = Field(default_factory=_default_worker_id, min_length=1)
    heartbeat_interval_seconds: float = Field(default=5.0, gt=0)
    lease_seconds: float = Field(default=30.0, gt=0)
    poll_interval_seconds: float = Field(default=1.0, gt=0)
    cancel_poll_interval_seconds: float = Field(default=0.2, gt=0)
    retry_base_delay_seconds: float = Field(default=30.0, ge=0)
    retry_jitter_seconds: float = Field(default=5.0, ge=0)
    max_model_repairs: int = Field(default=2, ge=0, le=5)
    model_timeout_seconds: float = Field(default=60.0, gt=0)
    model_provider: str = "fake"
    max_concurrent_tasks: int = Field(default=1, ge=1, le=16)

    @field_validator("worker_db_dsn")
    @classmethod
    def require_worker_database_role(cls, value: str) -> str:
        if urlsplit(value).username != "agentforge_worker":
            raise ValueError("worker database DSN must use the agentforge_worker role")
        return value

    @field_validator("business_ro_db_dsn")
    @classmethod
    def require_business_read_only_role(cls, value: str) -> str:
        if urlsplit(value).username != "agentforge_business_ro":
            raise ValueError(
                "business database DSN must use the agentforge_business_ro role"
            )
        return value

    @model_validator(mode="after")
    def require_heartbeat_before_expiry(self):
        if self.heartbeat_interval_seconds >= self.lease_seconds:
            raise ValueError("heartbeat interval must be shorter than the lease")
        return self

    model_config = {"env_prefix": "AGENTFORGE_"}
