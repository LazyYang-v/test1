"""One-time local Bootstrap and Operator recovery secret issuance."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import secrets
import sys
import uuid
from urllib.parse import urlsplit

import asyncpg

SECRET_TTL_SECONDS = 15 * 60
MIGRATOR_ROLE = "agentforge_migrator"


class BootstrapSecretError(RuntimeError):
    """A safe error for local Bootstrap and recovery secret operations."""


def _digest(secret: str) -> bytes:
    return hashlib.sha256(secret.encode("ascii")).digest()


def _validate_dsn(dsn: str | None) -> str:
    if not dsn:
        raise BootstrapSecretError("AGENTFORGE_MIGRATOR_DSN is required")
    if urlsplit(dsn).username != MIGRATOR_ROLE:
        raise BootstrapSecretError("migrator DSN must use the agentforge_migrator role")
    return dsn


async def _record_secret_issue(
    conn: asyncpg.Connection,
    *,
    secret: str,
    table: str,
    audit_action: str,
    target_user_id: str | None = None,
    recovery_action: str | None = None,
    rotate: bool = False,
) -> None:
    async with conn.transaction():
        await conn.execute(
            "SELECT pg_advisory_xact_lock(hashtext($1))",
            f"agentforge:{table}:issue",
        )
        issue_times = await conn.fetchrow(
            "SELECT clock_timestamp() AS issued_at, "
            "clock_timestamp() + make_interval(secs => $1::double precision) AS expires_at",
            SECRET_TTL_SECONDS,
        )
        issued_at = issue_times["issued_at"]
        expires_at = issue_times["expires_at"]

        if table == "identity.bootstrap_state":
            initialized = await conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM identity.operator_permissions)"
            )
            if initialized:
                raise BootstrapSecretError("Bootstrap is already initialized")
            row = await conn.fetchrow(
                "SELECT expires_at, consumed_at FROM identity.bootstrap_state "
                "WHERE singleton FOR UPDATE"
            )
            if row is not None and row["consumed_at"] is not None:
                raise BootstrapSecretError("Bootstrap is already consumed")
            if row is not None and row["expires_at"] > issued_at and not rotate:
                raise BootstrapSecretError("an unexpired Bootstrap secret already exists")
            await conn.execute(
                "INSERT INTO identity.bootstrap_state "
                "(singleton, secret_digest, issued_at, expires_at) "
                "VALUES (TRUE, $1, $2, $3) "
                "ON CONFLICT (singleton) DO UPDATE SET "
                "secret_digest = EXCLUDED.secret_digest, "
                "issued_at = EXCLUDED.issued_at, expires_at = EXCLUDED.expires_at, "
                "consumed_at = NULL, consumed_by = NULL",
                _digest(secret),
                issued_at,
                expires_at,
            )
            resource_id = "bootstrap"
            metadata = {}
        else:
            assert target_user_id is not None and recovery_action is not None
            target = await conn.fetchrow(
                "SELECT users.status, "
                "       count(permission.permission)::integer AS permission_count "
                "FROM identity.users AS users "
                "LEFT JOIN identity.operator_permissions AS permission "
                "  ON permission.user_id = users.id "
                "WHERE users.id = $1::uuid "
                "GROUP BY users.id",
                target_user_id,
            )
            if target is None or target["permission_count"] == 0:
                raise BootstrapSecretError("recovery target is not an Operator")
            required_status = "active" if recovery_action == "disable" else "disabled"
            if target["status"] != required_status:
                raise BootstrapSecretError("recovery target has an invalid status")
            row = await conn.fetchrow(
                "SELECT expires_at, consumed_at FROM identity.operator_recovery_state "
                "WHERE singleton FOR UPDATE"
            )
            if (
                row is not None
                and not rotate
                and (row["consumed_at"] is not None or row["expires_at"] > issued_at)
            ):
                raise BootstrapSecretError(
                    "an existing recovery secret requires explicit rotation"
                )
            await conn.execute(
                "INSERT INTO identity.operator_recovery_state "
                "(singleton, action, target_user_id, secret_digest, issued_at, expires_at) "
                "VALUES (TRUE, $1, $2::uuid, $3, $4, $5) "
                "ON CONFLICT (singleton) DO UPDATE SET "
                "action = EXCLUDED.action, target_user_id = EXCLUDED.target_user_id, "
                "secret_digest = EXCLUDED.secret_digest, issued_at = EXCLUDED.issued_at, "
                "expires_at = EXCLUDED.expires_at, consumed_at = NULL, consumed_by = NULL",
                recovery_action,
                target_user_id,
                _digest(secret),
                issued_at,
                expires_at,
            )
            resource_id = target_user_id
            metadata = {"recovery_action": recovery_action}

        await conn.execute(
            "INSERT INTO audit.audit_events "
            "(actor_type, action, resource_type, resource_id, decision, metadata_json) "
            "VALUES ('host_operator', $1, 'identity_control_secret', $2, 'allowed', $3::jsonb)",
            audit_action,
            resource_id,
            json.dumps(metadata, separators=(",", ":")),
        )


async def issue_bootstrap_secret(
    conn: asyncpg.Connection, *, rotate: bool = False
) -> str:
    """Store a digest for a new Bootstrap secret and return the secret once."""
    secret = secrets.token_urlsafe(32)
    await _record_secret_issue(
        conn,
        secret=secret,
        table="identity.bootstrap_state",
        audit_action=(
            "identity.bootstrap_secret.rotated"
            if rotate
            else "identity.bootstrap_secret.issued"
        ),
        rotate=rotate,
    )
    return secret


async def issue_operator_recovery_secret(
    conn: asyncpg.Connection,
    *,
    action: str,
    target_user_id: str,
    rotate: bool = False,
) -> str:
    """Issue a short-lived secret scoped to one existing Operator and action."""
    if action not in {"disable", "restore"}:
        raise BootstrapSecretError("recovery action must be disable or restore")
    try:
        normalized_user_id = str(uuid.UUID(target_user_id))
    except ValueError:
        raise BootstrapSecretError("recovery target must be a UUID") from None
    secret = secrets.token_urlsafe(32)
    await _record_secret_issue(
        conn,
        secret=secret,
        table="identity.operator_recovery_state",
        audit_action=(
            "identity.operator_recovery_secret.rotated"
            if rotate
            else "identity.operator_recovery_secret.issued"
        ),
        target_user_id=normalized_user_id,
        recovery_action=action,
        rotate=rotate,
    )
    return secret


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agentforge-bootstrap-secret")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("issue-bootstrap")
    commands.add_parser("rotate-bootstrap")
    recovery = commands.add_parser("issue-recovery")
    recovery.add_argument("--action", choices=("disable", "restore"), required=True)
    recovery.add_argument("--user-id", required=True)
    recovery.add_argument("--rotate", action="store_true")
    return parser


async def _run(args: argparse.Namespace) -> str:
    dsn = _validate_dsn(os.environ.get("AGENTFORGE_MIGRATOR_DSN"))
    conn = await asyncpg.connect(dsn)
    try:
        if args.command in {"issue-bootstrap", "rotate-bootstrap"}:
            return await issue_bootstrap_secret(
                conn, rotate=args.command == "rotate-bootstrap"
            )
        return await issue_operator_recovery_secret(
            conn,
            action=args.action,
            target_user_id=args.user_id,
            rotate=args.rotate,
        )
    finally:
        await conn.close()


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    if not sys.stdout.isatty():
        parser.error("secrets are displayed only to an interactive terminal")
    try:
        secret = asyncio.run(_run(args))
    except (BootstrapSecretError, asyncpg.PostgresError) as error:
        print(f"secret issuance failed: {error}", file=sys.stderr)
        raise SystemExit(1) from None
    print(secret)


if __name__ == "__main__":
    main()
