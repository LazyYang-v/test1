"""Liveness is a process-level property: it must answer 200 with no database at all."""

import pytest
from httpx import ASGITransport, AsyncClient


async def test_health_live_returns_200_without_any_database():
    from agentforge_api.app import create_app

    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "alive"}
