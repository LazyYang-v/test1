"""Environment-driven settings for the API process."""

import secrets
from urllib.parse import urlsplit

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """API runtime configuration.

    The DSN must target the ``agentforge_api`` login role; the API never
    connects as a superuser and never runs migrations.
    """

    api_db_dsn: str = "postgresql://agentforge_api@localhost:5432/agentforge"
    jwt_secret: str = Field(default_factory=lambda: secrets.token_urlsafe(48), min_length=32)
    jwt_ttl_seconds: int = Field(default=900, ge=1, le=900)
    live_evaluations_enabled: bool = False
    live_evaluation_call_budget_limit: int = Field(default=0, ge=0, le=1000)

    @field_validator("api_db_dsn")
    @classmethod
    def require_api_database_role(cls, value: str) -> str:
        username = urlsplit(value).username
        if username != "agentforge_api":
            raise ValueError("API database DSN must use the agentforge_api role")
        return value

    model_config = {"env_prefix": "AGENTFORGE_"}
