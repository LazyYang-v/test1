"""Configured task model providers use the same ModelAdapter protocol."""

import pytest
from pydantic import ValidationError

from agentforge_harness.models import (
    DeepSeekModel,
    FakeModel,
    ModelProviderConfigurationError,
)
from agentforge_worker.config import WorkerSettings
from agentforge_worker.model_factory import SnapshotModelAdapterFactory

WORKER_DSN = "postgresql://agentforge_worker:pw@localhost:5432/agentforge"


def test_fake_and_deepseek_are_selected_from_frozen_model_snapshot():
    settings = WorkerSettings(
        worker_db_dsn=WORKER_DSN,
        model_provider="fake",
        deepseek_api_key="test-key",
    )
    factory = SnapshotModelAdapterFactory(settings)

    fake = factory.resolve(
        {
            "provider": "deepseek",
            "model_name": "mutable-profile-value",
            "model_config_snapshot": {
                "provider": "fake",
                "model_name": "fake-frozen",
            },
        }
    )
    deepseek_settings = WorkerSettings(
        worker_db_dsn=WORKER_DSN,
        model_provider="deepseek",
        deepseek_api_key="test-key",
    )
    deepseek_factory = SnapshotModelAdapterFactory(deepseek_settings)
    deepseek = deepseek_factory.resolve(
        {
            "provider": "fake",
            "model_name": "mutable-profile-value",
            "model_config_snapshot": {
                "provider": "deepseek",
                "model_name": "deepseek-flash",
                "config_json": {"temperature": 0, "top_p": 0.9},
            },
        }
    )

    assert isinstance(fake, FakeModel)
    assert isinstance(deepseek, DeepSeekModel)
    assert deepseek.model_name == "deepseek-flash"
    assert deepseek_factory.resolve(
        {
            "model_config_snapshot": {
                "provider": "deepseek",
                "model_name": "deepseek-flash",
                "config_json": {"temperature": 0, "top_p": 0.9},
            }
        }
    ) is deepseek
    assert callable(getattr(fake, "generate"))
    assert callable(getattr(deepseek, "generate"))


@pytest.mark.parametrize("model_name", ["deepseek-flash", "deepseek-v4-pro"])
def test_factory_accepts_documented_deepseek_model_names(model_name):
    factory = SnapshotModelAdapterFactory(
        WorkerSettings(
            worker_db_dsn=WORKER_DSN,
            model_provider="deepseek",
            deepseek_api_key="unit-test-key",
        )
    )

    model = factory.resolve(
        {
            "model_config_snapshot": {
                "provider": "deepseek",
                "model_name": model_name,
            }
        }
    )

    assert isinstance(model, DeepSeekModel)
    assert model.model_name == model_name


async def test_fake_provider_does_not_require_or_initialize_live_credentials():
    factory = SnapshotModelAdapterFactory(WorkerSettings(worker_db_dsn=WORKER_DSN))

    model = factory.resolve(
        {"model_config_snapshot": {"provider": "fake", "model_name": "fake-v1"}}
    )

    assert isinstance(model, FakeModel)


def test_deepseek_configuration_fails_closed_without_a_key():
    with pytest.raises(ModelProviderConfigurationError, match="API_KEY"):
        SnapshotModelAdapterFactory(
            WorkerSettings(worker_db_dsn=WORKER_DSN, model_provider="deepseek")
        )


def test_worker_rejects_unsupported_provider_configuration():
    with pytest.raises(ValidationError):
        WorkerSettings(worker_db_dsn=WORKER_DSN, model_provider="other")


def test_worker_rejects_credential_or_http_deepseek_base_url():
    for url in (
        "http://api.deepseek.com",
        "https://user:secret@api.deepseek.com",
        "https://api.deepseek.com?key=secret",
    ):
        with pytest.raises(ValidationError):
            WorkerSettings(worker_db_dsn=WORKER_DSN, deepseek_base_url=url)
