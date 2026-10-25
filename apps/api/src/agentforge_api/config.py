"""Environment-driven settings for the API process."""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """API runtime configuration.

    The DSN must target the ``agentforge_api`` login role; the API never
    connects as a superuser and never runs migrations.
    """

    api_db_dsn: str = "postgresql://agentforge_api@localhost:5432/agentforge"

    model_config = {"env_prefix": "AGENTFORGE_"}
