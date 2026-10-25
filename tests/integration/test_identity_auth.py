"""Identity and authentication behavior through the HTTP API."""

from datetime import UTC, datetime

import asyncpg
import jwt
import pytest
from argon2 import PasswordHasher
from httpx import ASGITransport, AsyncClient

from agentforge_api.app import create_app
from agentforge_api.config import Settings

pytestmark = pytest.mark.integration
JWT_SECRET = "integration-test-signing-secret-at-least-32-bytes"


async def test_login_returns_short_lived_hs256_token_for_active_user(migrated_db):
    email = "alice@example.com"
    password = "correct horse battery staple"
    user_id = "0192a3b4-c5d6-7e8f-9012-3456789abcde"
    conn = await asyncpg.connect(migrated_db["role_dsn"]("migrator"))
    try:
        await conn.execute(
            """
            INSERT INTO identity.users
                (id, email, password_hash, display_name, status)
            VALUES ($1::uuid, $2, $3, $4, 'active')
            """,
            user_id,
            email,
            PasswordHasher().hash(password),
            "Alice Example",
        )
    finally:
        await conn.close()

    app = create_app(
        Settings(api_db_dsn=migrated_db["role_dsn"]("api"), jwt_secret=JWT_SECRET)
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.post(
            "/api/v1/auth/login", json={"email": email, "password": password}
        )

    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["expires_in"] <= 900
    claims = jwt.decode(body["access_token"], JWT_SECRET, algorithms=["HS256"])
    assert claims["sub"] == user_id
    assert claims["token_version"] == 1
    assert claims["exp"] - claims["iat"] <= 900
    assert datetime.fromtimestamp(claims["exp"], UTC) > datetime.now(UTC)


async def _create_user(migrated_db, *, status="active", token_version=1):
    user_id = "0192a3b4-c5d6-7e8f-9012-3456789abcde"
    conn = await asyncpg.connect(migrated_db["role_dsn"]("migrator"))
    try:
        await conn.execute(
            """
            INSERT INTO identity.users
                (id, email, password_hash, display_name, status, token_version)
            VALUES ($1::uuid, 'alice@example.com', $2, 'Alice Example', $3, $4)
            """,
            user_id,
            PasswordHasher().hash("correct horse battery staple"),
            status,
            token_version,
        )
    finally:
        await conn.close()
    return user_id


def _app(migrated_db):
    return create_app(
        Settings(api_db_dsn=migrated_db["role_dsn"]("api"), jwt_secret=JWT_SECRET)
    )


def _token(*, user_id, token_version=1, ttl=900):
    from agentforge_api.auth import create_access_token

    return create_access_token(
        user_id=user_id,
        token_version=token_version,
        secret=JWT_SECRET,
        ttl_seconds=ttl,
    )[0]


async def _audit_events(migrated_db):
    conn = await asyncpg.connect(migrated_db["role_dsn"]("migrator"))
    try:
        return await conn.fetch(
            "SELECT action, decision, reason_code FROM audit.audit_events ORDER BY id"
        )
    finally:
        await conn.close()


async def test_wrong_password_returns_uniform_unauthenticated_and_audits(migrated_db):
    await _create_user(migrated_db)
    app = _app(migrated_db)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.post(
            "/api/v1/auth/login",
            json={"email": "alice@example.com", "password": "wrong"},
            headers={"X-Request-ID": "auth-failure-request"},
        )

    assert response.status_code == 401
    assert response.json() == {
        "error": {
            "code": "UNAUTHENTICATED",
            "message": "Invalid email or password",
            "retryable": False,
            "details": {},
        },
        "request_id": "auth-failure-request",
    }
    assert [(row["action"], row["decision"], row["reason_code"]) for row in await _audit_events(migrated_db)] == [
        ("authentication.failed", "denied", "invalid_credentials")
    ]


async def test_disabled_user_cannot_log_in_and_failure_is_audited(migrated_db):
    await _create_user(migrated_db, status="disabled")
    app = _app(migrated_db)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.post(
            "/api/v1/auth/login",
            json={"email": "alice@example.com", "password": "correct horse battery staple"},
        )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHENTICATED"
    events = await _audit_events(migrated_db)
    assert [(row["action"], row["decision"]) for row in events] == [
        ("authentication.failed", "denied")
    ]


async def test_missing_expired_and_stale_credentials_are_uniformly_rejected(migrated_db):
    user_id = await _create_user(migrated_db)
    app = _app(migrated_db)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        missing = await client.get("/api/v1/auth/me")
        expired = await client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {_token(user_id=user_id, ttl=-1)}"},
        )
        stale = await client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {_token(user_id=user_id, token_version=2)}"},
        )

    assert missing.status_code == expired.status_code == stale.status_code == 401
    assert (
        missing.json()["error"]["code"]
        == expired.json()["error"]["code"]
        == stale.json()["error"]["code"]
        == "UNAUTHENTICATED"
    )


async def test_invalid_request_and_missing_route_use_uniform_error_envelope(migrated_db):
    app = _app(migrated_db)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        invalid = await client.post(
            "/api/v1/auth/login",
            json={"email": "alice@example.com"},
            headers={"X-Request-ID": "invalid-body-request"},
        )
        missing = await client.get(
            "/api/v1/no-such-route",
            headers={"X-Request-ID": "missing-route-request"},
        )

    assert invalid.status_code == 400
    assert invalid.json() == {
        "error": {
            "code": "INVALID_REQUEST",
            "message": "The request is invalid",
            "retryable": False,
            "details": {"fields": ["body.password"]},
        },
        "request_id": "invalid-body-request",
    }
    assert missing.status_code == 404
    assert missing.json() == {
        "error": {
            "code": "NOT_FOUND",
            "message": "The requested resource was not found",
            "retryable": False,
            "details": {},
        },
        "request_id": "missing-route-request",
    }


async def test_current_user_lists_only_active_region_grants(migrated_db):
    user_id = await _create_user(migrated_db)
    conn = await asyncpg.connect(migrated_db["role_dsn"]("migrator"))
    try:
        await conn.execute(
            """
            INSERT INTO identity.region_grants
                (id, user_id, region_code, permission, revoked_at)
            VALUES
                ('0192a3b4-c5d6-7e8f-9012-3456789ab001', $1::uuid, 'east', 'read', NULL),
                ('0192a3b4-c5d6-7e8f-9012-3456789ab002', $1::uuid, 'west', 'read', now())
            """,
            user_id,
        )
    finally:
        await conn.close()
    app = _app(migrated_db)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {_token(user_id=user_id)}"},
        )

    assert response.status_code == 200
    assert response.json() == {
        "id": user_id,
        "email": "alice@example.com",
        "display_name": "Alice Example",
        "regions": [{"region_code": "east", "permission": "read"}],
    }


async def test_region_grant_revocation_is_live_and_denial_is_audited(migrated_db):
    user_id = await _create_user(migrated_db)
    conn = await asyncpg.connect(migrated_db["role_dsn"]("migrator"))
    try:
        await conn.execute(
            """
            INSERT INTO identity.region_grants (id, user_id, region_code, permission)
            VALUES ('0192a3b4-c5d6-7e8f-9012-3456789ab001', $1::uuid, 'east', 'read')
            """,
            user_id,
        )
    finally:
        await conn.close()
    app = _app(migrated_db)
    token = _token(user_id=user_id)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        allowed = await client.get(
            "/api/v1/regions/east/access", headers={"Authorization": f"Bearer {token}"}
        )
        conn = await asyncpg.connect(migrated_db["role_dsn"]("migrator"))
        try:
            await conn.execute(
                "UPDATE identity.region_grants SET revoked_at = now() WHERE user_id = $1::uuid",
                user_id,
            )
        finally:
            await conn.close()
        denied = await client.get(
            "/api/v1/regions/east/access",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert allowed.status_code == 200
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "REGION_ACCESS_DENIED"
    assert [(row["action"], row["decision"], row["reason_code"]) for row in await _audit_events(migrated_db)] == [
        ("region.authorization", "denied", "REGION_ACCESS_DENIED")
    ]


async def test_token_version_change_invalidates_existing_token(migrated_db):
    user_id = await _create_user(migrated_db)
    app = _app(migrated_db)
    token = _token(user_id=user_id)
    conn = await asyncpg.connect(migrated_db["role_dsn"]("migrator"))
    try:
        await conn.execute(
            "UPDATE identity.users SET token_version = 2 WHERE id = $1::uuid", user_id
        )
    finally:
        await conn.close()

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.get(
            "/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"}
        )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHENTICATED"
