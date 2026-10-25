"""Database migration tooling: version bookkeeping and the SQL file runner.

Shared by the ``agentforge-migrate`` CLI and the API readiness check.
"""

from __future__ import annotations

import asyncpg

VERSION_TABLE_SCHEMA = "agentforge"
VERSION_TABLE = "agentforge.schema_migrations"

import re
from collections.abc import Mapping


class MigrationError(RuntimeError):
    """Raised when a migration file or its environment is invalid."""


_ENV_TOKEN = re.compile(r"\{\{env:([A-Za-z_][A-Za-z0-9_]*)\}\}")


def substitute_env(sql: str, env: Mapping[str, str]) -> str:
    """Replace ``{{env:NAME}}`` tokens with safely quoted SQL string literals.

    Missing variables raise MigrationError (fail closed: migrations must not
    silently create roles with empty or literal-placeholder passwords).
    """
    missing = sorted(
        {name for name in _ENV_TOKEN.findall(sql) if name not in env}
    )
    if missing:
        raise MigrationError(
            f"migration references unset environment variables: {', '.join(missing)}"
        )

    def _replace(match: re.Match[str]) -> str:
        value = env[match.group(1)]
        return "'" + value.replace("'", "''") + "'"

    return _ENV_TOKEN.sub(_replace, sql)


async def fetch_applied_version(conn: asyncpg.Connection) -> int | None:
    """Return the highest applied migration version, or None if none applied.

    Raises asyncpg's UndefinedSchemaError/UndefinedTableError when migrations
    have never run — the readiness check maps that to "not_migrated".
    """
    row = await conn.fetchrow(f"SELECT MAX(version) AS version FROM {VERSION_TABLE}")
    if row is None or row["version"] is None:
        return None
    return int(row["version"])
