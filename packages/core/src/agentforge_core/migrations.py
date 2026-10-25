"""Database migration tooling: version bookkeeping and the SQL file runner.

Shared by the ``agentforge-migrate`` CLI and the API readiness check.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from collections.abc import Mapping
from pathlib import Path

import asyncpg

VERSION_TABLE_SCHEMA = "agentforge"
VERSION_TABLE = "agentforge.schema_migrations"
MIGRATOR_ROLE = "agentforge_migrator"


class MigrationError(RuntimeError):
    """Raised when a migration file or its environment is invalid."""


_ENV_TOKEN = re.compile(r"\{\{env:([A-Za-z_][A-Za-z0-9_]*)\}\}")
_FILENAME = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")


def substitute_env(sql: str, env: Mapping[str, str]) -> str:
    """Replace ``{{env:NAME}}`` tokens with safely quoted SQL string literals.

    Missing variables raise MigrationError (fail closed: migrations must not
    silently create roles with empty or literal-placeholder passwords).
    """
    missing = sorted({name for name in _ENV_TOKEN.findall(sql) if name not in env})
    if missing:
        raise MigrationError(
            f"migration references unset environment variables: {', '.join(missing)}"
        )

    def _replace(match: re.Match[str]) -> str:
        value = env[match.group(1)]
        return "'" + value.replace("'", "''") + "'"

    return _ENV_TOKEN.sub(_replace, sql)


def _discover(versions_dir: Path) -> list[tuple[int, str, Path]]:
    if not versions_dir.is_dir():
        raise MigrationError(f"migrations directory not found: {versions_dir}")
    found: dict[int, tuple[int, str, Path]] = {}
    for path in sorted(versions_dir.iterdir()):
        match = _FILENAME.match(path.name)
        if match is None:
            raise MigrationError(f"unrecognized migration filename: {path}")
        version = int(match.group(1))
        if version in found:
            raise MigrationError(f"duplicate migration version: {version:04d}")
        found[version] = (version, path.name, path)
    return [found[key] for key in sorted(found)]


async def fetch_applied_version(conn: asyncpg.Connection) -> int | None:
    """Return the highest applied migration version, or None if none applied.

    Raises asyncpg's UndefinedSchemaError/UndefinedTableError when migrations
    have never run — the readiness check maps that to "not_migrated".
    """
    row = await conn.fetchrow(f"SELECT MAX(version) AS version FROM {VERSION_TABLE}")
    if row is None or row["version"] is None:
        return None
    return int(row["version"])


async def apply_migrations(
    dsn: str, versions_dir: Path | str, env: Mapping[str, str]
) -> list[int]:
    """Apply pending migrations in order; return the versions applied.

    Connects as the migrator role (never a superuser). Each migration runs in
    a single transaction together with its bookkeeping row. The version table
    is ensured first, so a fresh database converges to migrated.
    """
    migrations = _discover(Path(versions_dir))

    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(
            "SELECT pg_advisory_lock(hashtext('agentforge_migrate'))"
        )
        await conn.execute(f"CREATE SCHEMA IF NOT EXISTS {VERSION_TABLE_SCHEMA}")
        await conn.execute(
            f"CREATE TABLE IF NOT EXISTS {VERSION_TABLE} ("
            "version INTEGER PRIMARY KEY, "
            "name TEXT NOT NULL, "
            "checksum TEXT NOT NULL, "
            "applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
        )
        columns = await conn.fetch(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = $1 AND table_name = $2",
            VERSION_TABLE_SCHEMA,
            "schema_migrations",
        )
        has_checksum = "checksum" in {row["column_name"] for row in columns}
        rows = await conn.fetch(f"SELECT version, name FROM {VERSION_TABLE}")
        if rows and not has_checksum:
            raise MigrationError(
                "migration ledger has applied rows without checksums; refusing to "
                "backfill from current files. Manually verify and reconcile all "
                "previously applied migrations before recreating the ledger"
            )
        if not has_checksum:
            await conn.execute(
                f"ALTER TABLE {VERSION_TABLE} ADD COLUMN checksum "
                "TEXT NOT NULL DEFAULT ''"
            )
        applied = {
            int(row["version"]): (row["name"], row["checksum"])
            for row in await conn.fetch(
                f"SELECT version, name, checksum FROM {VERSION_TABLE}"
            )
        }
        for version in range(1, max(applied, default=0) + 1):
            if version not in applied:
                raise MigrationError(
                    f"migration ledger is not contiguous: missing version {version:04d}"
                )
        migration_by_version = {
            version: (name, path) for version, name, path in migrations
        }
        for version, (applied_name, applied_checksum) in applied.items():
            migration = migration_by_version.get(version)
            if migration is None:
                raise MigrationError(
                    f"applied migration {version:04d} ({applied_name}) no longer exists"
                )
            name, path = migration
            if applied_name != name:
                raise MigrationError(
                    f"migration {version:04d} filename mismatch: ledger records "
                    f"{applied_name}, current file is {name}"
                )
            checksum = hashlib.sha256(path.read_bytes()).hexdigest()
            if applied_checksum != checksum:
                raise MigrationError(
                    f"migration {name} checksum mismatch: file changed after it was applied"
                )

        if migrations and migrations[0][0] == 0:
            raise MigrationError("migration files must start at version 0001")
        for version in range(1, max(migration_by_version, default=0) + 1):
            if version not in migration_by_version:
                raise MigrationError(
                    f"migration files are not contiguous: missing version {version:04d}"
                )

        newly: list[int] = []
        for version, name, path in migrations:
            if version in applied:
                continue
            source = path.read_bytes()
            checksum = hashlib.sha256(source).hexdigest()
            sql = substitute_env(source.decode("utf-8"), env)
            async with conn.transaction():
                await conn.execute(sql)
                await conn.execute(
                    f"INSERT INTO {VERSION_TABLE} (version, name, checksum) "
                    "VALUES ($1, $2, $3)",
                    version,
                    name,
                    checksum,
                )
            newly.append(version)
        return newly
    finally:
        # Closing the session releases its advisory lock on success and failure.
        await conn.close()


def main() -> None:
    """CLI entry point for the compose ``migrate`` service."""
    import argparse
    import os
    import sys

    parser = argparse.ArgumentParser(
        prog="agentforge-migrate",
        description="Apply AgentForge SQL migrations as the migrator role",
    )
    parser.add_argument(
        "--dir",
        type=Path,
        default=Path("migrations/versions"),
        help="directory containing NNNN_name.sql migrations",
    )
    args = parser.parse_args()
    env = os.environ

    try:
        dsn = env.get("AGENTFORGE_MIGRATOR_DSN")
        if not dsn:
            raise MigrationError("AGENTFORGE_MIGRATOR_DSN is required")
        applied = asyncio.run(apply_migrations(dsn, args.dir, env))
    except MigrationError as error:
        print(f"migration failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error

    if applied:
        print("applied: " + ", ".join(f"{version:04d}" for version in applied))
    else:
        print("up to date")
