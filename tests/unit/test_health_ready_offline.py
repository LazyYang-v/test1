"""Ready must refuse (503) when the database is unreachable — offline via a closed port."""

from httpx import ASGITransport, AsyncClient

from agentforge_api.app import create_app
from agentforge_api.config import Settings


async def test_health_ready_returns_503_when_database_unreachable():
    settings = Settings(api_db_dsn="postgresql://agentforge_api:x@127.0.0.1:1/agentforge")
    app = create_app(settings)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        response = await client.get("/health/ready")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "unavailable"
    assert body["reason"] == "db_unreachable"
