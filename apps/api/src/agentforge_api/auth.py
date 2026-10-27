"""Authentication helpers for short-lived API bearer tokens."""

from __future__ import annotations

import logging
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

import anyio
import asyncpg
import jwt
from agentforge_core.observability import log_event
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from agentforge_api.rate_limit import TokenBucketLimiter

logger = logging.getLogger("agentforge.api")

_PASSWORDS = PasswordHasher(time_cost=2, memory_cost=19_456, parallelism=1)
_DUMMY_PASSWORD_HASH = _PASSWORDS.hash(secrets.token_urlsafe(32))
_BEARER = HTTPBearer(auto_error=False)


class ApiError(Exception):
    """An error that is safe to return through the public API."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.retryable = retryable
        self.details = details or {}
        self.headers = headers or {}


def create_access_token(
    *, user_id: str, token_version: int, secret: str, ttl_seconds: int
) -> tuple[str, int]:
    """Sign a bounded-lifetime HS256 token and return it with its TTL."""
    issued_at = datetime.now(UTC)
    expires_at = issued_at + timedelta(seconds=min(ttl_seconds, 900))
    token = jwt.encode(
        {
            "sub": user_id,
            "token_version": token_version,
            "iat": issued_at,
            "exp": expires_at,
        },
        secret,
        algorithm="HS256",
    )
    return token, int((expires_at - issued_at).total_seconds())


async def verify_password(password: str, password_hash: str | None) -> bool:
    """Verify Argon2id without blocking the async server loop."""
    candidate_hash = password_hash or _DUMMY_PASSWORD_HASH
    try:
        return await anyio.to_thread.run_sync(_PASSWORDS.verify, candidate_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


async def hash_password(password: str) -> str:
    """Hash a new password with the same Argon2id settings used for login."""
    return await anyio.to_thread.run_sync(_PASSWORDS.hash, password)


async def operator_permission_allowed(
    conn: asyncpg.Connection,
    request: Request,
    principal: dict[str, Any],
    permission: str,
) -> bool:
    """Check a live Operator permission while holding a shared user-row lock."""
    if permission not in {"identity_admin", "registry_operator"}:
        raise ValueError("unknown Operator permission")
    user = await conn.fetchrow(
        """
        SELECT users.status, users.token_version,
               EXISTS (
                   SELECT 1 FROM identity.operator_permissions AS permissions
                   WHERE permissions.user_id = users.id
                     AND permissions.permission = $2
               ) AS permitted
        FROM identity.users AS users
        WHERE users.id = $1::uuid
        FOR SHARE OF users
        """,
        principal["id"],
        permission,
    )
    allowed = bool(
        user is not None
        and user["status"] == "active"
        and user["token_version"] == principal["token_version"]
        and user["permitted"]
    )
    if not allowed:
        await conn.execute(
            """
            INSERT INTO audit.audit_events
                (actor_type, actor_id, action, resource_type, resource_id,
                 decision, reason_code, metadata_json, trace_id)
            VALUES ('user', $1::uuid, 'operator.authorization', 'operator_permission',
                    $2, 'denied', 'OPERATOR_PERMISSION_REQUIRED',
                    jsonb_build_object('permission', $2::text), $3)
            """,
            principal["id"],
            permission,
            request.state.request_id,
        )
    return allowed


async def log_auth_failure(
    conn: asyncpg.Connection, request: Request, reason_code: str
) -> None:
    await conn.execute(
        """
        INSERT INTO audit.audit_events
            (actor_type, action, resource_type, decision, reason_code, metadata_json, trace_id)
        VALUES ('anonymous', 'authentication.failed', 'identity', 'denied', $1,
                '{}'::jsonb, $2)
        """,
        reason_code,
        request.state.request_id,
    )


async def current_principal(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_BEARER),
) -> dict[str, Any]:
    """Validate JWT and recheck user status/version against PostgreSQL."""
    if credentials is None or credentials.scheme.lower() != "bearer":
        async with request.app.state.connect() as conn:
            await log_auth_failure(conn, request, "missing_credentials")
        raise ApiError(401, "UNAUTHENTICATED", "Authentication required")

    try:
        claims = jwt.decode(
            credentials.credentials,
            request.app.state.settings.jwt_secret,
            algorithms=["HS256"],
            options={"require": ["sub", "iat", "exp", "token_version"]},
        )
        user_id = str(claims["sub"])
        token_version = int(claims["token_version"])
    except (jwt.InvalidTokenError, KeyError, TypeError, ValueError) as error:
        async with request.app.state.connect() as conn:
            reason_code = "expired_token" if isinstance(error, jwt.ExpiredSignatureError) else "invalid_token"
            await log_auth_failure(conn, request, reason_code)
        raise ApiError(401, "UNAUTHENTICATED", "Authentication required") from None

    async with request.app.state.connect() as conn:
        user = await conn.fetchrow(
            """
            SELECT id::text AS id, email, display_name, status, token_version
            FROM identity.users WHERE id = $1::uuid
            """,
            user_id,
        )
        if (
            user is None
            or user["status"] != "active"
            or user["token_version"] != token_version
        ):
            await log_auth_failure(conn, request, "inactive_or_stale_token")
            raise ApiError(401, "UNAUTHENTICATED", "Authentication required")
    limiter: TokenBucketLimiter = request.app.state.rate_limiter
    allowed, retry_after = limiter.consume(str(user["id"]))
    if not allowed:
        log_event(
            logger,
            logging.WARNING,
            "rate_limited",
            request_id=request.state.request_id,
            error_code="RATE_LIMITED",
        )
        raise ApiError(
            429,
            "RATE_LIMITED",
            "Request rate limit exceeded",
            retryable=True,
            headers={"Retry-After": str(retry_after)},
        )
    return dict(user)
