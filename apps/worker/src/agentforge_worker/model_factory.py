"""Resolve the model adapter from the task's immutable model snapshot."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

from agentforge_harness.models import (
    DeepSeekModel,
    FakeModel,
    ModelAdapter,
    ModelProviderConfigurationError,
)

from agentforge_worker.config import WorkerSettings


class SnapshotModelAdapterFactory:
    """Build one shared-protocol adapter for each loaded agent snapshot."""

    def __init__(self, settings: WorkerSettings) -> None:
        if settings.model_provider == "deepseek" and not settings.deepseek_api_key:
            raise ModelProviderConfigurationError(
                "AGENTFORGE_DEEPSEEK_API_KEY is required when DeepSeek is configured"
            )
        self._settings = settings
        self._fake = FakeModel()
        self._deepseek_models: dict[tuple[str, ...], DeepSeekModel] = {}

    def resolve(self, agent: dict[str, Any]) -> ModelAdapter:
        snapshot = agent.get("model_config_snapshot")
        if not isinstance(snapshot, dict):
            snapshot = {}
        provider = snapshot.get("provider", agent.get("provider"))
        model_name = snapshot.get(
            "model_name", snapshot.get("model", agent.get("model_name"))
        )
        config_json = snapshot.get("config_json")
        if isinstance(config_json, str):
            try:
                config_json = json.loads(config_json)
            except json.JSONDecodeError as error:
                raise ModelProviderConfigurationError(
                    "frozen model parameters are invalid JSON"
                ) from error
        if config_json is not None:
            snapshot = {**snapshot, "config_json": config_json}
        if not isinstance(provider, str) or not provider:
            raise ModelProviderConfigurationError("agent model provider is missing")
        if not isinstance(model_name, str) or not model_name:
            raise ModelProviderConfigurationError("agent model name is missing")
        if provider != self._settings.model_provider:
            raise ModelProviderConfigurationError(
                "task provider differs from AGENTFORGE_MODEL_PROVIDER; configure "
                "the worker to match the frozen agent model snapshot"
            )
        if provider == "fake":
            return self._fake
        if provider != "deepseek":
            raise ModelProviderConfigurationError(
                f"model provider {provider!r} is not supported"
            )
        if not self._settings.deepseek_api_key:
            raise ModelProviderConfigurationError(
                "AGENTFORGE_DEEPSEEK_API_KEY is required for DeepSeek tasks"
            )
        if model_name not in {"deepseek-chat", "deepseek-reasoner"}:
            raise ModelProviderConfigurationError(
                f"DeepSeek model {model_name!r} is not supported"
            )
        parameters = snapshot.get("config_json", {})
        if not isinstance(parameters, dict):
            raise ModelProviderConfigurationError(
                "frozen DeepSeek parameters must be an object"
            )
        cache_key = (
            model_name,
            self._settings.deepseek_base_url,
            str(self._settings.deepseek_api_key),
            str(
                snapshot.get(
                    "input_cost_per_million",
                    self._settings.deepseek_input_cost_per_million,
                )
            ),
            str(
                snapshot.get(
                    "output_cost_per_million",
                    self._settings.deepseek_output_cost_per_million,
                )
            ),
            json.dumps(parameters, sort_keys=True, separators=(",", ":")),
        )
        for cost_key in ("input_cost_per_million", "output_cost_per_million"):
            value = snapshot.get(cost_key, 0)
            try:
                amount = Decimal(str(value))
            except Exception as error:
                raise ModelProviderConfigurationError(
                    f"frozen {cost_key} must be numeric"
                ) from error
            if not amount.is_finite() or amount < 0:
                raise ModelProviderConfigurationError(
                    f"frozen {cost_key} must be finite and non-negative"
                )
        model = self._deepseek_models.get(cache_key)
        if model is None:
            model = DeepSeekModel(
                model_name=model_name,
                api_key=self._settings.deepseek_api_key,
                base_url=self._settings.deepseek_base_url,
                input_cost_per_million=Decimal(
                    str(snapshot.get("input_cost_per_million", 0))
                ),
                output_cost_per_million=Decimal(
                    str(snapshot.get("output_cost_per_million", 0))
                ),
            )
            self._deepseek_models[cache_key] = model
        return model

    async def aclose(self) -> None:
        for model in self._deepseek_models.values():
            await model.aclose()
