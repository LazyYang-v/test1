"""Migration 0001 against real PostgreSQL: schemas, roles, idempotency, privileges."""

import asyncio
import hashlib
from pathlib import Path

import asyncpg
import pytest

from agentforge_core.evaluation import CORE_CASES
from agentforge_core.migrations import MigrationError, apply_migrations
from agentforge_core.schema_version import SCHEMA_VERSION

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
    "agentforge_registry_cli",
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


async def test_creates_all_six_roles_all_non_superuser(migrated_db):
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
    assert by_name["agentforge_registry_cli"]["rolcreaterole"] is False
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


async def test_replay_after_clearing_bookkeeping_fails_closed(migrated_db):
    """A wiped ledger is tampering, not a recovery path.

    Contract (2026-10-27 decision): normal re-runs are ledgered no-ops, and a
    missing/tampered ledger must fail closed. Shipped migrations are not
    required to be raw-SQL replayable, so clearing
    ``agentforge.schema_migrations`` and re-running the migrator must raise —
    never present itself as a successful recovery. The run must also stay
    fail-closed afterwards: every committed migration keeps its ledger row,
    no partially applied migration is recorded, key schema objects survive,
    and the ledger never claims the full schema again.
    """
    dsn = migrated_db["role_dsn"]("migrator")
    conn = await _connect(dsn)
    try:
        await conn.execute("DELETE FROM agentforge.schema_migrations")
    finally:
        await conn.close()

    # Replay hits migration 0009's unguarded ADD CONSTRAINT on an
    # already-migrated schema and must abort, not silently converge.
    with pytest.raises(asyncpg.DuplicateObjectError, match="tasks_trace_id_nonempty"):
        await apply_migrations(dsn, VERSIONS_DIR, migrated_db["migrate_env"])

    conn = await _connect(dsn)
    try:
        rows = await conn.fetch(
            "SELECT version FROM agentforge.schema_migrations ORDER BY version"
        )
        schema_count = await conn.fetchval(
            "SELECT count(*) FROM pg_namespace WHERE nspname = ANY($1::text[])",
            sorted(EXPECTED_SCHEMAS),
        )
        later_objects = await conn.fetchval(
            "SELECT to_regclass('evaluation.datasets')::text"
        )
        case_count = await conn.fetchval("SELECT count(*) FROM evaluation.cases")
    finally:
        await conn.close()

    # Per-migration atomicity held: only fully committed migrations are in the
    # ledger, as a contiguous prefix strictly below this build's expected
    # version — the readiness probe will keep refusing this database.
    applied = [row["version"] for row in rows]
    assert applied == list(range(1, len(applied) + 1))
    assert not applied or applied[-1] < SCHEMA_VERSION

    # Schema and data survived the aborted replay untouched: all seven
    # schemas, objects from post-ledger migrations, and the seeded benchmark
    # cases are intact — the schema is deliberately ahead of the rewound
    # ledger, which is why recovery requires manual reconciliation.
    assert schema_count == len(EXPECTED_SCHEMAS)
    assert later_objects == "evaluation.datasets"
    assert case_count == len(CORE_CASES)

    # The tampering is permanent for the migrator: a subsequent normal run
    # must still fail closed instead of "healing" the database.
    with pytest.raises(asyncpg.DuplicateObjectError):
        await apply_migrations(dsn, VERSIONS_DIR, migrated_db["migrate_env"])


async def test_gapped_migration_directory_fails_before_applying_any_file(
    unmigrated_db, tmp_path
):
    (tmp_path / "0001_first.sql").write_text(
        "CREATE TABLE public.migration_gap_effect (version INTEGER NOT NULL); "
        "INSERT INTO public.migration_gap_effect VALUES (1);",
        encoding="utf-8",
    )
    (tmp_path / "0003_third.sql").write_text(
        "INSERT INTO public.migration_gap_effect VALUES (3);", encoding="utf-8"
    )

    with pytest.raises(
        MigrationError,
        match=r"migration files are not contiguous: missing version 0002",
    ):
        await apply_migrations(unmigrated_db["superuser_dsn"], tmp_path, {})

    conn = await _connect(unmigrated_db["superuser_dsn"])
    try:
        table = await conn.fetchval(
            "SELECT to_regclass('public.migration_gap_effect')"
        )
    finally:
        await conn.close()
    assert table is None


async def test_gapped_migration_ledger_fails_before_applying_pending_migration(
    unmigrated_db, tmp_path
):
    dsn = unmigrated_db["superuser_dsn"]
    conn = await _connect(dsn)
    try:
        await conn.execute(
            "CREATE TABLE public.migration_gap_executions (version INTEGER NOT NULL)"
        )
    finally:
        await conn.close()

    (tmp_path / "0001_first.sql").write_text(
        "INSERT INTO public.migration_gap_executions VALUES (1);", encoding="utf-8"
    )
    (tmp_path / "0002_second.sql").write_text(
        "INSERT INTO public.migration_gap_executions VALUES (2);", encoding="utf-8"
    )
    await apply_migrations(dsn, tmp_path, {})

    conn = await _connect(dsn)
    try:
        await conn.execute(
            "DELETE FROM agentforge.schema_migrations WHERE version = 1"
        )
    finally:
        await conn.close()

    with pytest.raises(
        MigrationError,
        match=r"migration ledger is not contiguous: missing version 0001",
    ):
        await apply_migrations(dsn, tmp_path, {})

    conn = await _connect(dsn)
    try:
        executions = await conn.fetchval(
            "SELECT count(*) FROM public.migration_gap_executions"
        )
        ledger_versions = await conn.fetch(
            "SELECT version FROM agentforge.schema_migrations ORDER BY version"
        )
    finally:
        await conn.close()
    assert executions == 2
    assert [row["version"] for row in ledger_versions] == [2]


async def test_api_role_reads_version_table_but_cannot_create_tables(migrated_db):
    conn = await _connect(migrated_db["role_dsn"]("api"))
    try:
        version = await conn.fetchval(
            "SELECT MAX(version) FROM agentforge.schema_migrations"
        )
        assert version == SCHEMA_VERSION

        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await conn.execute("CREATE TABLE identity.forbidden (id INTEGER)")
    finally:
        await conn.close()
