"""Database migration tooling: version bookkeeping and the SQL file runner.

Shared by the ``agentforge-migrate`` CLI and the API readiness check.
"""

from __future__ import annotations

import asyncpg

VERSION_TABLE_SCHEMA = "agentforge"
VERSION_TABLE = "agentforge.schema_migrations"


async def fetch_applied_version(conn: asyncpg.Connection) -> int | None:
    """Return the highest applied migration version, or None if none applied.

    Raises asyncpg's UndefinedSchemaError/UndefinedTableError when migrations
    have never run — the readiness check maps that to "not_migrated".
    """
    row = await conn.fetchrow(f"SELECT MAX(version) AS version FROM {VERSION_TABLE}")
    if row is None or row["version"] is None:
        return None
    return int(row["version"])
