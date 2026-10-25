"""Readiness probe: validates migration version and database reachability.

Ready means more than "the port answers": the database must be reachable and
its applied schema version must exactly match what this build expects.
"""

from typing import Any

import asyncpg

from agentforge_core.migrations import fetch_applied_version
from agentforge_core.schema_version import SCHEMA_VERSION, evaluate_readiness

CONNECT_TIMEOUT_SECONDS = 5.0


async def check_database_readiness(dsn: str) -> dict[str, Any]:
    expected = SCHEMA_VERSION
    unavailable: dict[str, Any] = {
        "status": "unavailable",
        "expected_schema_version": expected,
        "applied_schema_version": None,
    }

    try:
        conn = await asyncpg.connect(dsn, timeout=CONNECT_TIMEOUT_SECONDS)
    except Exception:
        return {**unavailable, "reason": "db_unreachable"}

    try:
        try:
            applied = await fetch_applied_version(conn)
        except asyncpg.UndefinedTableError:
            return {**unavailable, "reason": "not_migrated"}
        except asyncpg.PostgresError:
            return {**unavailable, "reason": "db_error"}
    finally:
        await conn.close()

    decision = evaluate_readiness(expected, applied)
    if decision == "ok":
        return {
            "status": "ready",
            "expected_schema_version": expected,
            "applied_schema_version": applied,
        }
    return {**unavailable, "reason": decision, "applied_schema_version": applied}
