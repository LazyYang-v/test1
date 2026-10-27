"""Identity and authentication behavior through the HTTP API."""

import sys
from datetime import UTC, datetime

import asyncpg
import jwt
import pytest
from agentforge_api.app import create_app
from agentforge_api.config import Settings
from agentforge_core.bootstrap import (
    issue_bootstrap_secret,
    issue_operator_recovery_secret,
)
from agentforge_core.bootstrap import (
    main as bootstrap_secret_main,
)
from argon2 import PasswordHasher
from httpx import ASGITransport, AsyncClient

pytestmark = pytest.mark.integration
JWT_SECRET = "integration-test-signing-secret-at-least-32-bytes"


async def test_migration_0013_scopes_identity_and_registry_privileges(migrated_db):
    conn = await asyncpg.connect(migrated_db["role_dsn"]("migrator"))
    try:
        privileges = await conn.fetchrow(
            """
            SELECT
                has_table_privilege('agentforge_api',
                    'identity.operator_permissions', 'SELECT') AS operator_select,
                has_table_privilege('agentforge_api',
                    'identity.operator_permissions', 'INSERT') AS operator_insert,
                has_table_privilege('agentforge_api',
                    'identity.operator_permissions', 'UPDATE') AS operator_update,
                has_column_privilege('agentforge_api',
                    'identity.bootstrap_state', 'consumed_at', 'UPDATE') AS bootstrap_consume,
                has_column_privilege('agentforge_api',
                    'identity.bootstrap_state', 'secret_digest', 'UPDATE') AS bootstrap_rotate,
                has_column_privilege('agentforge_api',
                    'identity.users', 'password_hash', 'UPDATE') AS user_password_update,
                has_column_privilege('agentforge_api',
                    'identity.users', 'email', 'UPDATE') AS user_email_update,
                has_column_privilege('agentforge_api',
                    'registry.agent_versions', 'model_config_snapshot', 'UPDATE') AS agent_publish,
                has_column_privilege('agentforge_api',
                    'registry.agent_versions', 'created_by', 'UPDATE') AS agent_actor_update,
                has_table_privilege('agentforge_registry_cli',
                    'registry.agent_versions', 'INSERT,UPDATE,DELETE') AS cli_write
            """
        )
    finally:
        await conn.close()

    assert dict(privileges) == {
        "operator_select": True,
        "operator_insert": True,
        "operator_update": False,
        "bootstrap_consume": True,
        "bootstrap_rotate": False,
        "user_password_update": True,
        "user_email_update": False,
        "agent_publish": True,
        "agent_actor_update": False,
        "cli_write": False,
    }


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
        "operator_permissions": [],
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


async def _create_operator(
    migrated_db,
    *,
    user_id="0192a3b4-c5d6-7e8f-9012-3456789ac001",
    email="operator@example.com",
    password="original-operator-password",
):
    conn = await asyncpg.connect(migrated_db["role_dsn"]("migrator"))
    try:
        await conn.execute(
            "INSERT INTO identity.users "
            "(id, email, password_hash, display_name, status, token_version) "
            "VALUES ($1::uuid, $2, $3, 'Test Operator', 'active', 1)",
            user_id,
            email,
            PasswordHasher().hash(password),
        )
        await conn.executemany(
            "INSERT INTO identity.operator_permissions (user_id, permission) "
            "VALUES ($1::uuid, $2)",
            [(user_id, "identity_admin"), (user_id, "registry_operator")],
        )
    finally:
        await conn.close()
    return user_id


async def _audit_metadata(migrated_db):
    conn = await asyncpg.connect(migrated_db["role_dsn"]("migrator"))
    try:
        return await conn.fetch(
            "SELECT action, decision, metadata_json::text AS metadata "
            "FROM audit.audit_events ORDER BY id"
        )
    finally:
        await conn.close()


def test_bootstrap_secret_cli_refuses_redirected_output(monkeypatch, capsys):
    monkeypatch.setattr(
        sys, "argv", ["agentforge-bootstrap-secret", "issue-bootstrap"]
    )

    with pytest.raises(SystemExit) as error:
        bootstrap_secret_main()

    captured = capsys.readouterr()
    assert error.value.code == 2
    assert captured.out == ""
    assert "interactive terminal" in captured.err


async def test_bootstrap_secret_creates_one_operator_and_is_audited(migrated_db):
    conn = await asyncpg.connect(migrated_db["role_dsn"]("migrator"))
    try:
        secret = await issue_bootstrap_secret(conn)
    finally:
        await conn.close()

    app = _app(migrated_db)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        bootstrap = await client.post(
            "/api/v1/bootstrap",
            json={
                "bootstrap_secret": secret,
                "email": "First.Operator@example.com",
                "display_name": "First Operator",
                "initial_password": "first-operator-password",
            },
        )
        reused = await client.post(
            "/api/v1/bootstrap",
            json={
                "bootstrap_secret": secret,
                "email": "another@example.com",
                "display_name": "Another Operator",
                "initial_password": "another-operator-password",
            },
        )
        login = await client.post(
            "/api/v1/auth/login",
            json={
                "email": "first.operator@example.com",
                "password": "first-operator-password",
            },
        )
        profile = await client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {login.json()['access_token']}"},
        )

    assert bootstrap.status_code == 201
    assert bootstrap.json()["operator_permissions"] == [
        "identity_admin",
        "registry_operator",
    ]
    assert reused.status_code == 409
    assert reused.json()["error"]["code"] == "BOOTSTRAP_CONSUMED"
    assert login.status_code == 200
    assert profile.status_code == 200
    assert profile.json()["operator_permissions"] == [
        "identity_admin",
        "registry_operator",
    ]
    audit = await _audit_metadata(migrated_db)
    assert [(row["action"], row["decision"]) for row in audit] == [
        ("identity.bootstrap_secret.issued", "allowed"),
        ("identity.bootstrap.completed", "allowed"),
        ("identity.bootstrap", "denied"),
    ]
    assert all(secret not in row["metadata"] for row in audit)


async def test_operator_creates_restricted_business_user(migrated_db):
    operator_id = await _create_operator(migrated_db)
    app = _app(migrated_db)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        operator_login = await client.post(
            "/api/v1/auth/login",
            json={
                "email": "operator@example.com",
                "password": "original-operator-password",
            },
        )
        operator_token = operator_login.json()["access_token"]
        created = await client.post(
            "/api/v1/admin/identity/users",
            json={
                "email": "business@example.com",
                "display_name": "Business User",
                "initial_password": "business-user-password",
            },
            headers={"Authorization": f"Bearer {operator_token}"},
        )
        duplicate = await client.post(
            "/api/v1/admin/identity/users",
            json={
                "email": "business@example.com",
                "display_name": "Duplicate Business User",
                "initial_password": "another-business-password",
            },
            headers={"Authorization": f"Bearer {operator_token}"},
        )
        business_login = await client.post(
            "/api/v1/auth/login",
            json={
                "email": "business@example.com",
                "password": "business-user-password",
            },
        )
        business_profile = await client.get(
            "/api/v1/auth/me",
            headers={
                "Authorization": f"Bearer {business_login.json()['access_token']}"
            },
        )
        denied = await client.post(
            "/api/v1/admin/identity/users",
            json={
                "email": "second-business@example.com",
                "display_name": "Second Business User",
                "initial_password": "second-business-password",
            },
            headers={
                "Authorization": f"Bearer {business_login.json()['access_token']}"
            },
        )

    assert operator_login.status_code == 200
    assert created.status_code == 201
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "EMAIL_ALREADY_EXISTS"
    assert created.json()["operator_permissions"] == []
    assert created.json()["regions"] == [
        {"region_code": "CN_EAST", "permission": "read"}
    ]
    assert business_profile.status_code == 200
    assert business_profile.json()["operator_permissions"] == []
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "OPERATOR_PERMISSION_REQUIRED"

    conn = await asyncpg.connect(migrated_db["role_dsn"]("migrator"))
    try:
        grants = await conn.fetch(
            "SELECT region_code, permission FROM identity.region_grants "
            "WHERE user_id = $1::uuid AND revoked_at IS NULL",
            created.json()["id"],
        )
        permissions = await conn.fetchval(
            "SELECT count(*) FROM identity.operator_permissions WHERE user_id = $1::uuid",
            created.json()["id"],
        )
    finally:
        await conn.close()
    assert [dict(grant) for grant in grants] == [
        {"region_code": "CN_EAST", "permission": "read"}
    ]
    assert permissions == 0
    assert operator_id == "0192a3b4-c5d6-7e8f-9012-3456789ac001"


async def test_operator_permissions_are_checked_live(migrated_db):
    operator_id = await _create_operator(migrated_db)
    app = _app(migrated_db)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        login = await client.post(
            "/api/v1/auth/login",
            json={
                "email": "operator@example.com",
                "password": "original-operator-password",
            },
        )
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
        allowed = await client.post(
            "/api/v1/admin/identity/users",
            json={
                "email": "first-business@example.com",
                "display_name": "First Business User",
                "initial_password": "first-business-password",
            },
            headers=headers,
        )
        conn = await asyncpg.connect(migrated_db["role_dsn"]("migrator"))
        try:
            await conn.execute(
                "DELETE FROM identity.operator_permissions "
                "WHERE user_id = $1::uuid AND permission = 'identity_admin'",
                operator_id,
            )
        finally:
            await conn.close()
        denied = await client.post(
            "/api/v1/admin/identity/users",
            json={
                "email": "second-business@example.com",
                "display_name": "Second Business User",
                "initial_password": "second-business-password",
            },
            headers=headers,
        )

    assert allowed.status_code == 201
    assert denied.status_code == 403


async def test_operator_disable_and_one_time_recovery(migrated_db):
    operator_id = await _create_operator(migrated_db)
    app = _app(migrated_db)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        login = await client.post(
            "/api/v1/auth/login",
            json={
                "email": "operator@example.com",
                "password": "original-operator-password",
            },
        )
        old_token = login.json()["access_token"]
        disabled = await client.post(
            f"/api/v1/admin/identity/operators/{operator_id}/disable",
            headers={"Authorization": f"Bearer {old_token}"},
        )
        old_session = await client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {old_token}"},
        )

    assert disabled.status_code == 200
    assert disabled.json()["status"] == "disabled"
    assert old_session.status_code == 401

    conn = await asyncpg.connect(migrated_db["role_dsn"]("migrator"))
    try:
        recovery_secret = await issue_operator_recovery_secret(
            conn, action="restore", target_user_id=operator_id
        )
    finally:
        await conn.close()

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        restored = await client.post(
            "/api/v1/operator-recovery",
            json={
                "recovery_secret": recovery_secret,
                "new_password": "replacement-operator-password",
            },
        )
        replay = await client.post(
            "/api/v1/operator-recovery",
            json={
                "recovery_secret": recovery_secret,
                "new_password": "replacement-operator-password",
            },
        )
        new_login = await client.post(
            "/api/v1/auth/login",
            json={
                "email": "operator@example.com",
                "password": "replacement-operator-password",
            },
        )

    assert restored.status_code == 200
    assert restored.json()["status"] == "active"
    assert replay.status_code == 409
    assert replay.json()["error"]["code"] == "RECOVERY_CONSUMED"
    assert new_login.status_code == 200
    conn = await asyncpg.connect(migrated_db["role_dsn"]("migrator"))
    try:
        disable_secret = await issue_operator_recovery_secret(
            conn, action="disable", target_user_id=operator_id, rotate=True
        )
    finally:
        await conn.close()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        disabled_again = await client.post(
            "/api/v1/operator-recovery",
            json={"recovery_secret": disable_secret},
        )
    assert disabled_again.status_code == 200
    assert disabled_again.json()["status"] == "disabled"

    conn = await asyncpg.connect(migrated_db["role_dsn"]("migrator"))
    try:
        restore_secret = await issue_operator_recovery_secret(
            conn, action="restore", target_user_id=operator_id, rotate=True
        )
    finally:
        await conn.close()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        restored_again = await client.post(
            "/api/v1/operator-recovery",
            json={
                "recovery_secret": restore_secret,
                "new_password": "final-operator-password",
            },
        )
        final_login = await client.post(
            "/api/v1/auth/login",
            json={
                "email": "operator@example.com",
                "password": "final-operator-password",
            },
        )
    assert restored_again.status_code == 200
    assert restored_again.json()["status"] == "active"
    assert final_login.status_code == 200

    audit = await _audit_metadata(migrated_db)
    assert all(
        secret not in row["metadata"]
        for secret in (recovery_secret, disable_secret, restore_secret)
        for row in audit
    )
    assert any(row["action"] == "identity.operator.disabled" for row in audit)
    assert any(row["action"] == "identity.operator.recovered" for row in audit)
