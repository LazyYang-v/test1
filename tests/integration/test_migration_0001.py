"""Migration 0001 against real PostgreSQL: schemas, roles, idempotency, privileges."""

import asyncio
import hashlib
from pathlib import Path

import asyncpg
import pytest

from agentforge_core.migrations import MigrationError, apply_migrations

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


async def test_ledger_checksum_hashes_raw_file_contents(unmigrated_db, tmp_path):
    source = b"SELECT {{env:VALUE}};\n"
    (tmp_path / "0001_initial.sql").write_bytes(source)

    await apply_migrations(
        unmigrated_db["superuser_dsn"], tmp_path, {"VALUE": "substituted"}
    )

    conn = await _connect(unmigrated_db["superuser_dsn"])
    try:
        checksum = await conn.fetchval(
            "SELECT checksum FROM agentforge.schema_migrations WHERE version = 1"
        )
    finally:
        await conn.close()
    assert checksum == hashlib.sha256(source).hexdigest()


async def test_modified_applied_migration_fails_closed(unmigrated_db, tmp_path):
    migration = tmp_path / "0001_initial.sql"
    migration.write_text("SELECT 1;", encoding="utf-8")

    await apply_migrations(unmigrated_db["superuser_dsn"], tmp_path, {})
    migration.write_text("SELECT 2;", encoding="utf-8")

    with pytest.raises(MigrationError, match="checksum"):
        await apply_migrations(unmigrated_db["superuser_dsn"], tmp_path, {})


async def test_renamed_applied_migration_fails_closed(unmigrated_db, tmp_path):
    migration = tmp_path / "0001_initial.sql"
    migration.write_text("SELECT 1;", encoding="utf-8")
    await apply_migrations(unmigrated_db["superuser_dsn"], tmp_path, {})

    migration.rename(tmp_path / "0001_renamed.sql")

    with pytest.raises(MigrationError, match="filename mismatch"):
        await apply_migrations(unmigrated_db["superuser_dsn"], tmp_path, {})


async def test_missing_applied_migration_version_fails_closed(unmigrated_db, tmp_path):
    migration = tmp_path / "0001_initial.sql"
    migration.write_text("SELECT 1;", encoding="utf-8")
    dsn = unmigrated_db["superuser_dsn"]
    await apply_migrations(dsn, tmp_path, {})

    migration.rename(tmp_path / "0002_renamed_version.sql")

    with pytest.raises(MigrationError, match="no longer exists"):
        await apply_migrations(dsn, tmp_path, {})


async def test_concurrent_runners_apply_migration_once(unmigrated_db, tmp_path):
    migration = tmp_path / "0001_initial.sql"
    migration.write_text(
        "CREATE TABLE public.migration_execution_count (n INTEGER NOT NULL); "
        "INSERT INTO public.migration_execution_count VALUES (1);",
        encoding="utf-8",
    )
    dsn = unmigrated_db["superuser_dsn"]

    first, second = await asyncio.gather(
        apply_migrations(dsn, tmp_path, {}),
        apply_migrations(dsn, tmp_path, {}),
    )

    conn = await _connect(dsn)
    try:
        executions = await conn.fetchval(
            "SELECT count(*) FROM public.migration_execution_count"
        )
        ledger_entries = await conn.fetchval(
            "SELECT count(*) FROM agentforge.schema_migrations WHERE version = 1"
        )
    finally:
        await conn.close()
    assert sorted((first, second), key=len) == [[], [1]]
    assert executions == 1
    assert ledger_entries == 1


async def test_legacy_ledger_with_applied_rows_requires_manual_reconciliation(
    unmigrated_db, tmp_path
):
    migration = tmp_path / "0001_initial.sql"
    migration.write_text("SELECT 1;", encoding="utf-8")
    conn = await _connect(unmigrated_db["superuser_dsn"])
    try:
        await conn.execute("CREATE SCHEMA agentforge")
        await conn.execute(
            "CREATE TABLE agentforge.schema_migrations ("
            "version INTEGER PRIMARY KEY, name TEXT NOT NULL, "
            "applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
        )
        await conn.execute(
            "INSERT INTO agentforge.schema_migrations (version, name) "
            "VALUES (1, '0001_initial.sql')"
        )
    finally:
        await conn.close()

    with pytest.raises(MigrationError, match="refusing to backfill"):
        await apply_migrations(unmigrated_db["superuser_dsn"], tmp_path, {})


async def test_raw_reexecution_after_clearing_bookkeeping_succeeds(migrated_db):
    conn = await _connect(migrated_db["role_dsn"]("migrator"))
    try:
        await conn.execute("DELETE FROM agentforge.schema_migrations")
    finally:
        await conn.close()

    applied = await apply_migrations(
        migrated_db["role_dsn"]("migrator"), VERSIONS_DIR, migrated_db["migrate_env"]
    )
    assert applied == [1, 2]

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
    assert version == 2
    assert schema_count == len(EXPECTED_SCHEMAS)


async def test_api_role_reads_version_table_but_cannot_create_tables(migrated_db):
    conn = await _connect(migrated_db["role_dsn"]("api"))
    try:
        version = await conn.fetchval(
            "SELECT MAX(version) FROM agentforge.schema_migrations"
        )
        assert version == 2

        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await conn.execute("CREATE TABLE identity.forbidden (id INTEGER)")
    finally:
        await conn.close()
