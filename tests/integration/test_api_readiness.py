"""The API readiness probe checks real PostgreSQL migration state."""

import asyncpg
import pytest
from httpx import ASGITransport, AsyncClient

from agentforge_api.app import create_app
from agentforge_api.config import Settings


pytestmark = pytest.mark.integration


async def _get_ready(dsn: str):
    app = create_app(Settings(api_db_dsn=dsn))
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        return await client.get("/health/ready")


async def test_ready_refuses_database_without_migration(unmigrated_db, migrated_db):
    # The API role exists on the cluster after the baseline migration, so grant
    # it access to the empty ledger in this separate, fresh database.
    conn = await asyncpg.connect(unmigrated_db["superuser_dsn"])
    try:
        await conn.execute(
            "CREATE SCHEMA agentforge; "
            "CREATE TABLE agentforge.schema_migrations "
            "(version INTEGER PRIMARY KEY, name TEXT NOT NULL, checksum TEXT NOT NULL, "
            "applied_at TIMESTAMPTZ NOT NULL DEFAULT now()); "
            "GRANT USAGE ON SCHEMA agentforge TO agentforge_api; "
            "GRANT SELECT ON agentforge.schema_migrations TO agentforge_api"
        )
    finally:
        await conn.close()
    response = await _get_ready(unmigrated_db["role_dsn"]("api"))

    assert response.status_code == 503
    assert response.json()["reason"] == "not_migrated"


async def test_ready_rejects_gapped_migration_ledger(migrated_db):
    conn = await asyncpg.connect(migrated_db["role_dsn"]("migrator"))
    try:
        await conn.execute("DELETE FROM agentforge.schema_migrations WHERE version = 2")
    finally:
        await conn.close()

    response = await _get_ready(migrated_db["role_dsn"]("api"))

    assert response.status_code == 503
    assert response.json()["reason"] == "version_mismatch"


async def test_ready_requires_exact_migration_version(migrated_db):
    response = await _get_ready(migrated_db["role_dsn"]("api"))
    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "expected_schema_version": 7,
        "applied_schema_version": 7,
    }

    conn = await asyncpg.connect(migrated_db["role_dsn"]("migrator"))
    try:
        await conn.execute(
            "UPDATE agentforge.schema_migrations SET version = 8 WHERE version = 7"
        )
    finally:
        await conn.close()

    mismatch = await _get_ready(migrated_db["role_dsn"]("api"))
    assert mismatch.status_code == 503
    assert mismatch.json()["reason"] == "version_mismatch"
