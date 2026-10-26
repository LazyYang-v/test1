"""Authentication helpers for short-lived API bearer tokens."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

import anyio
import asyncpg
import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

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
    return dict(user)
