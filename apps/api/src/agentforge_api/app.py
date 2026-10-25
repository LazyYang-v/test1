"""FastAPI application factory for the AgentForge control plane."""

from fastapi import FastAPI

from agentforge_api.config import Settings


def create_app(settings: Settings | None = None) -> FastAPI:
    app_settings = settings or Settings()

    app = FastAPI(title="AgentForge API")

    @app.get("/health/live")
    async def health_live() -> dict[str, str]:
        return {"status": "alive"}

    return app


app = create_app()
