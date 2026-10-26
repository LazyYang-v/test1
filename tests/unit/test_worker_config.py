"""Worker defaults and role/heartbeat guardrails."""

import pytest
from pydantic import ValidationError

from agentforge_worker.config import WorkerSettings

WORKER_DSN = "postgresql://agentforge_worker:pw@localhost:5432/agentforge"
BUSINESS_RO_DSN = (
    "postgresql://agentforge_business_ro:pw@localhost:5432/agentforge"
)


def test_default_heartbeat_and_lease_match_the_p0_contract():
    settings = WorkerSettings(worker_db_dsn=WORKER_DSN)
    assert settings.heartbeat_interval_seconds == 5
    assert settings.lease_seconds == 30
    assert settings.business_ro_statement_timeout_ms == 3000


def test_worker_dsn_must_use_the_worker_role():
    with pytest.raises(ValidationError):
        WorkerSettings(worker_db_dsn="postgresql://agentforge_api:pw@localhost/db")


def test_business_ro_dsn_must_use_the_business_ro_role():
    settings = WorkerSettings(
        worker_db_dsn=WORKER_DSN, business_ro_db_dsn=BUSINESS_RO_DSN
    )
    assert settings.business_ro_db_dsn == BUSINESS_RO_DSN

    for dsn in (
        "postgresql://agentforge_worker:pw@localhost/db",
        "postgresql://agentforge_migrator:pw@localhost/db",
        "postgresql://localhost/db",
    ):
        with pytest.raises(ValidationError):
            WorkerSettings(worker_db_dsn=WORKER_DSN, business_ro_db_dsn=dsn)


def test_business_ro_statement_timeout_is_short_and_positive():
    for value in (0, -1, 60_000):
        with pytest.raises(ValidationError):
            WorkerSettings(
                worker_db_dsn=WORKER_DSN, business_ro_statement_timeout_ms=value
            )


def test_heartbeat_must_be_shorter_than_the_lease():
    with pytest.raises(ValidationError):
        WorkerSettings(
            worker_db_dsn=WORKER_DSN,
            heartbeat_interval_seconds=30,
            lease_seconds=30,
        )
