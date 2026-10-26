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


def test_model_provider_configuration_defaults_to_fake_without_credentials():
    settings = WorkerSettings(worker_db_dsn=WORKER_DSN)

    assert settings.model_provider == "fake"
    assert settings.deepseek_api_key is None
    assert settings.deepseek_base_url == "https://api.deepseek.com/v1"
    assert settings.deepseek_input_cost_per_million == 0
    assert settings.deepseek_output_cost_per_million == 0


def test_model_provider_must_be_supported_and_deepseek_key_is_explicit():
    settings = WorkerSettings(
        worker_db_dsn=WORKER_DSN,
        model_provider="deepseek",
        deepseek_api_key="configured-in-test",
    )
    assert settings.model_provider == "deepseek"
    assert settings.deepseek_api_key == "configured-in-test"

    with pytest.raises(ValidationError):
        WorkerSettings(worker_db_dsn=WORKER_DSN, model_provider="unknown")


def test_deepseek_token_costs_must_be_non_negative_and_finite():
    for field, value in (
        ("deepseek_input_cost_per_million", -1),
        ("deepseek_output_cost_per_million", float("inf")),
    ):
        with pytest.raises(ValidationError):
            WorkerSettings(worker_db_dsn=WORKER_DSN, **{field: value})


def test_heartbeat_must_be_shorter_than_the_lease():
    with pytest.raises(ValidationError):
        WorkerSettings(
            worker_db_dsn=WORKER_DSN,
            heartbeat_interval_seconds=30,
            lease_seconds=30,
        )
