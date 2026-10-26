"""Worker defaults and role/heartbeat guardrails."""

import pytest
from pydantic import ValidationError

from agentforge_worker.config import WorkerSettings

WORKER_DSN = "postgresql://agentforge_worker:pw@localhost:5432/agentforge"


def test_default_heartbeat_and_lease_match_the_p0_contract():
    settings = WorkerSettings(worker_db_dsn=WORKER_DSN)
    assert settings.heartbeat_interval_seconds == 5
    assert settings.lease_seconds == 30


def test_worker_dsn_must_use_the_worker_role():
    with pytest.raises(ValidationError):
        WorkerSettings(worker_db_dsn="postgresql://agentforge_api:pw@localhost/db")


def test_heartbeat_must_be_shorter_than_the_lease():
    with pytest.raises(ValidationError):
        WorkerSettings(
            worker_db_dsn=WORKER_DSN,
            heartbeat_interval_seconds=30,
            lease_seconds=30,
        )
