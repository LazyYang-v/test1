"""Migration 0001 against real PostgreSQL: schemas, roles, idempotency, privileges."""

from pathlib import Path

import asyncpg
import pytest

from agentforge_core.migrations import apply_migrations

VERSIONS_DIR = Path(__file__).resolve().parents[2] / "migrations" / "versions"

pytestmark = pytest.mark.integration

EXPECTED_SCHEMAS = {
    "identity",
    "registry",
    "runtime",
    "harness",
    "business",
    "evaluation",
    "audit",
}
EXPECTED_ROLES = {
    "agentforge_migrator",
    "agentforge_api",
    "agentforge_worker",
    "agentforge_business_ro",
    "agentforge_eval",
}


async def _connect(dsn: str) -> asyncpg.Connection:
    return await asyncpg.connect(dsn)


async def test_creates_all_seven_schemas(migrated_db):
    conn = await _connect(migrated_db["role_dsn"]("migrator"))
    try:
        rows = await conn.fetch(
            "SELECT nspname FROM pg_namespace WHERE nspname = ANY($1::text[])",
            sorted(EXPECTED_SCHEMAS),
        )
    finally:
        await conn.close()
    assert {row["nspname"] for row in rows} == EXPECTED_SCHEMAS


async def test_creates_all_five_roles_all_non_superuser(migrated_db):
    conn = await _connect(migrated_db["superuser_dsn"])
    try:
        rows = await conn.fetch(
            """
            SELECT rolname, rolsuper, rolcanlogin, rolcreaterole
            FROM pg_roles WHERE rolname = ANY($1::text[])
            """,
            sorted(EXPECTED_ROLES),
        )
    finally:
        await conn.close()
    by_name = {row["rolname"]: row for row in rows}
    assert set(by_name) == EXPECTED_ROLES
    for name, row in by_name.items():
        assert row["rolsuper"] is False, name
        assert row["rolcanlogin"] is True, name
    assert by_name["agentforge_migrator"]["rolcreaterole"] is True
    assert by_name["agentforge_api"]["rolcreaterole"] is False


async def test_reapplying_is_a_noop(migrated_db):
    applied = await apply_migrations(
        migrated_db["role_dsn"]("migrator"), VERSIONS_DIR, migrated_db["migrate_env"]
    )
    assert applied == []


async def test_raw_reexecution_after_clearing_bookkeeping_succeeds(migrated_db):
    conn = await _connect(migrated_db["role_dsn"]("migrator"))
    try:
        await conn.execute("DELETE FROM agentforge.schema_migrations")
    finally:
        await conn.close()

    applied = await apply_migrations(
        migrated_db["role_dsn"]("migrator"), VERSIONS_DIR, migrated_db["migrate_env"]
    )
    assert applied == [1]

    conn = await _connect(migrated_db["role_dsn"]("migrator"))
    try:
        version = await conn.fetchval(
            "SELECT MAX(version) FROM agentforge.schema_migrations"
        )
        schema_count = await conn.fetchval(
            "SELECT count(*) FROM pg_namespace WHERE nspname = ANY($1::text[])",
            sorted(EXPECTED_SCHEMAS),
        )
    finally:
        await conn.close()
    assert version == 1
    assert schema_count == len(EXPECTED_SCHEMAS)


async def test_api_role_reads_version_table_but_cannot_create_tables(migrated_db):
    conn = await _connect(migrated_db["role_dsn"]("api"))
    try:
        version = await conn.fetchval(
            "SELECT MAX(version) FROM agentforge.schema_migrations"
        )
        assert version == 1

        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await conn.execute("CREATE TABLE identity.forbidden (id INTEGER)")
    finally:
        await conn.close()
