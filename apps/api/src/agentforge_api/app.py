"""FastAPI application factory for the AgentForge control plane."""

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from agentforge_api.config import Settings
from agentforge_api.readiness import check_database_readiness


def create_app(settings: Settings | None = None) -> FastAPI:
    app_settings = settings or Settings()

    app = FastAPI(title="AgentForge API")

    @app.get("/health/live")
    async def health_live() -> dict[str, str]:
        return {"status": "alive"}

    @app.get("/health/ready")
    async def health_ready() -> JSONResponse:
        body = await check_database_readiness(app_settings.api_db_dsn)
        status_code = 200 if body["status"] == "ready" else 503
        return JSONResponse(status_code=status_code, content=body)

    return app


app = create_app()
