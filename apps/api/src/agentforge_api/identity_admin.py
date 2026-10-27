"""Bootstrap, identity provisioning, and Operator recovery endpoints."""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from typing import Any

import asyncpg
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from agentforge_api.auth import (
    ApiError,
    current_principal,
    hash_password,
    operator_permission_allowed,
)

router = APIRouter()


def _validate_control_secret(value: SecretStr) -> SecretStr:
    secret = value.get_secret_value()
    if not secret.isascii() or not all(char.isalnum() or char in "-_" for char in secret):
        raise ValueError("secret must use URL-safe ASCII characters")
    return value


class _SecretRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bootstrap_secret: SecretStr = Field(min_length=43, max_length=128)
    email: str = Field(min_length=3, max_length=320)
    display_name: str = Field(min_length=1, max_length=160)
    initial_password: SecretStr

    @field_validator("bootstrap_secret")
    @classmethod
    def validate_bootstrap_secret(cls, value: SecretStr) -> SecretStr:
        return _validate_control_secret(value)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not normalized:
            raise ValueError("email must not be blank")
        return normalized

    @field_validator("display_name")
    @classmethod
    def normalize_display_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("display_name must not be blank")
        return normalized

    @field_validator("initial_password")
    @classmethod
    def validate_initial_password(cls, value: SecretStr) -> SecretStr:
        password = value.get_secret_value()
        if not 12 <= len(password) <= 1024:
            raise ValueError("initial_password must contain 12 to 1024 characters")
        return value


class CreateUserRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=3, max_length=320)
    display_name: str = Field(min_length=1, max_length=160)
    initial_password: SecretStr

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not normalized:
            raise ValueError("email must not be blank")
        return normalized

    @field_validator("display_name")
    @classmethod
    def normalize_display_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("display_name must not be blank")
        return normalized

    @field_validator("initial_password")
    @classmethod
    def validate_initial_password(cls, value: SecretStr) -> SecretStr:
        password = value.get_secret_value()
        if not 12 <= len(password) <= 1024:
            raise ValueError("initial_password must contain 12 to 1024 characters")
        return value


class OperatorRecoveryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    recovery_secret: SecretStr = Field(min_length=43, max_length=128)
    new_password: SecretStr | None = None

    @field_validator("recovery_secret")
    @classmethod
    def validate_recovery_secret(cls, value: SecretStr) -> SecretStr:
        return _validate_control_secret(value)

    @field_validator("new_password")
    @classmethod
    def validate_new_password(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and not 12 <= len(value.get_secret_value()) <= 1024:
            raise ValueError("new_password must contain 12 to 1024 characters")
        return value


def _secret_digest(secret: str) -> bytes:
    return hashlib.sha256(secret.encode("ascii")).digest()


async def _audit(
    conn: asyncpg.Connection,
    *,
    actor_type: str,
    actor_id: str | None,
    action: str,
    resource_type: str,
    resource_id: str | None,
    decision: str,
    request: Request,
    reason_code: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    await conn.execute(
        """
        INSERT INTO audit.audit_events
            (actor_type, actor_id, action, resource_type, resource_id, decision,
             reason_code, metadata_json, trace_id)
        VALUES ($1, $2::uuid, $3, $4, $5, $6, $7, $8::jsonb, $9)
        """,
        actor_type,
        actor_id,
        action,
        resource_type,
        resource_id,
        decision,
        reason_code,
        json.dumps(metadata or {}, sort_keys=True, separators=(",", ":")),
        request.state.request_id,
    )


async def _bootstrap_rejection(
    request: Request, *, status: int, code: str, message: str
) -> ApiError:
    async with request.app.state.connect() as conn:
        await _audit(
            conn,
            actor_type="anonymous",
            actor_id=None,
            action="identity.bootstrap",
            resource_type="bootstrap",
            resource_id=None,
            decision="denied",
            reason_code=code,
            request=request,
        )
    return ApiError(status, code, message)


async def _recovery_rejection(
    request: Request, *, status: int, code: str, message: str
) -> ApiError:
    async with request.app.state.connect() as conn:
        await _audit(
            conn,
            actor_type="anonymous",
            actor_id=None,
            action="identity.operator.recovery",
            resource_type="operator",
            resource_id=None,
            decision="denied",
            reason_code=code,
            request=request,
        )
    return ApiError(status, code, message)



@router.post("/api/v1/bootstrap", status_code=201)
async def bootstrap_operator(body: _SecretRequest, request: Request) -> dict[str, Any]:
    secret = body.bootstrap_secret.get_secret_value()
    password = body.initial_password.get_secret_value()
    digest = _secret_digest(secret)

    async with request.app.state.connect() as conn:
        state = await conn.fetchrow(
            "SELECT secret_digest, expires_at, consumed_at, "
            "expires_at <= clock_timestamp() AS expired "
            "FROM identity.bootstrap_state WHERE singleton = TRUE"
        )
        if state is None or not hmac.compare_digest(bytes(state["secret_digest"]), digest):
            raise await _bootstrap_rejection(
                request,
                status=403,
                code="BOOTSTRAP_UNAVAILABLE",
                message="Bootstrap is unavailable",
            )
        if state["expired"]:
            raise await _bootstrap_rejection(
                request,
                status=410,
                code="BOOTSTRAP_EXPIRED",
                message="Bootstrap has expired",
            )
        if state["consumed_at"] is not None:
            raise await _bootstrap_rejection(
                request,
                status=409,
                code="BOOTSTRAP_CONSUMED",
                message="Bootstrap has already been consumed",
            )
        existing_operator = await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM identity.operator_permissions)"
        )
        if existing_operator:
            raise await _bootstrap_rejection(
                request,
                status=409,
                code="BOOTSTRAP_ALREADY_INITIALIZED",
                message="Bootstrap has already been initialized",
            )
        existing_email = await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM identity.users WHERE email = $1)",
            body.email,
        )
        if existing_email:
            raise await _bootstrap_rejection(
                request,
                status=409,
                code="EMAIL_ALREADY_EXISTS",
                message="Email already exists",
            )

    password_hash = await hash_password(password)
    user_id = str(uuid.uuid4())
    failure: ApiError | None = None
    async with request.app.state.connect() as conn:
        try:
            async with conn.transaction():
                locked = await conn.fetchrow(
                    "SELECT secret_digest, expires_at, consumed_at, "
                    "expires_at <= clock_timestamp() AS expired "
                    "FROM identity.bootstrap_state WHERE singleton = TRUE FOR UPDATE"
                )
                if (
                    locked is None
                    or not hmac.compare_digest(bytes(locked["secret_digest"]), digest)
                ):
                    failure = ApiError(403, "BOOTSTRAP_UNAVAILABLE", "Bootstrap is unavailable")
                elif locked["expired"]:
                    failure = ApiError(410, "BOOTSTRAP_EXPIRED", "Bootstrap has expired")
                elif locked["consumed_at"] is not None:
                    failure = ApiError(409, "BOOTSTRAP_CONSUMED", "Bootstrap has already been consumed")
                elif await conn.fetchval(
                    "SELECT EXISTS (SELECT 1 FROM identity.operator_permissions)"
                ):
                    failure = ApiError(
                        409,
                        "BOOTSTRAP_ALREADY_INITIALIZED",
                        "Bootstrap has already been initialized",
                    )
                elif await conn.fetchval(
                    "SELECT EXISTS (SELECT 1 FROM identity.users WHERE email = $1)",
                    body.email,
                ):
                    failure = ApiError(409, "EMAIL_ALREADY_EXISTS", "Email already exists")

                if failure is not None:
                    await _audit(
                        conn,
                        actor_type="anonymous",
                        actor_id=None,
                        action="identity.bootstrap",
                        resource_type="bootstrap",
                        resource_id=None,
                        decision="denied",
                        reason_code=failure.code,
                        request=request,
                    )
                else:
                    await conn.execute(
                        "INSERT INTO identity.users "
                        "(id, email, password_hash, display_name, status, token_version) "
                        "VALUES ($1::uuid, $2, $3, $4, 'active', 1)",
                        user_id,
                        body.email,
                        password_hash,
                        body.display_name,
                    )
                    await conn.executemany(
                        "INSERT INTO identity.operator_permissions (user_id, permission) "
                        "VALUES ($1::uuid, $2)",
                        [(user_id, "identity_admin"), (user_id, "registry_operator")],
                    )
                    await _audit(
                        conn,
                        actor_type="bootstrap",
                        actor_id=user_id,
                        action="identity.bootstrap.completed",
                        resource_type="operator",
                        resource_id=user_id,
                        decision="allowed",
                        request=request,
                        metadata={
                            "operator_permissions": [
                                "identity_admin",
                                "registry_operator",
                            ]
                        },
                    )
                    await conn.execute(
                        "UPDATE identity.bootstrap_state "
                        "SET consumed_at = clock_timestamp(), consumed_by = $1::uuid "
                        "WHERE singleton = TRUE",
                        user_id,
                    )
        except asyncpg.UniqueViolationError:
            failure = ApiError(409, "EMAIL_ALREADY_EXISTS", "Email already exists")
            async with request.app.state.connect() as audit_conn:
                await _audit(
                    audit_conn,
                    actor_type="anonymous",
                    actor_id=None,
                    action="identity.bootstrap",
                    resource_type="bootstrap",
                    resource_id=None,
                    decision="denied",
                    reason_code=failure.code,
                    request=request,
                )

    if failure is not None:
        raise failure
    return {
        "id": user_id,
        "email": body.email,
        "display_name": body.display_name,
        "operator_permissions": ["identity_admin", "registry_operator"],
    }


@router.post("/api/v1/admin/identity/users", status_code=201)
async def create_business_user(
    body: CreateUserRequest,
    request: Request,
    principal: dict[str, Any] = Depends(current_principal),
) -> dict[str, Any]:
    user_id = str(uuid.uuid4())
    failure: ApiError | None = None

    async with request.app.state.connect() as conn:
        async with conn.transaction():
            if not await operator_permission_allowed(
                conn, request, principal, "identity_admin"
            ):
                failure = ApiError(
                    403,
                    "OPERATOR_PERMISSION_REQUIRED",
                    "Operator permission is required",
                    details={"permission": "identity_admin"},
                )
            elif await conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM identity.users WHERE email = $1)",
                body.email,
            ):
                failure = ApiError(409, "EMAIL_ALREADY_EXISTS", "Email already exists")
                await _audit(
                    conn,
                    actor_type="user",
                    actor_id=principal["id"],
                    action="identity.user.create",
                    resource_type="user",
                    resource_id=None,
                    decision="denied",
                    reason_code=failure.code,
                    request=request,
                )
            else:
                password_hash = await hash_password(
                    body.initial_password.get_secret_value()
                )
                created_user_id = await conn.fetchval(
                    """
                    INSERT INTO identity.users
                        (id, email, password_hash, display_name, status, token_version)
                    VALUES ($1::uuid, $2, $3, $4, 'active', 1)
                    ON CONFLICT (email) DO NOTHING
                    RETURNING id::text
                    """,
                    user_id,
                    body.email,
                    password_hash,
                    body.display_name,
                )
                if created_user_id is None:
                    failure = ApiError(409, "EMAIL_ALREADY_EXISTS", "Email already exists")
                    await _audit(
                        conn,
                        actor_type="user",
                        actor_id=principal["id"],
                        action="identity.user.create",
                        resource_type="user",
                        resource_id=None,
                        decision="denied",
                        reason_code=failure.code,
                        request=request,
                    )
                else:
                    grant_id = str(uuid.uuid4())
                    await conn.execute(
                        "INSERT INTO identity.region_grants "
                        "(id, user_id, region_code, permission) "
                        "VALUES ($1::uuid, $2::uuid, 'CN_EAST', 'read')",
                        grant_id,
                        user_id,
                    )
                    await _audit(
                        conn,
                        actor_type="user",
                        actor_id=principal["id"],
                        action="identity.user.created",
                        resource_type="user",
                        resource_id=user_id,
                        decision="allowed",
                        request=request,
                        metadata={"status": "active"},
                    )
                    await _audit(
                        conn,
                        actor_type="user",
                        actor_id=principal["id"],
                        action="identity.region_grant.created",
                        resource_type="region_grant",
                        resource_id=grant_id,
                        decision="allowed",
                        request=request,
                        metadata={"region_code": "CN_EAST", "permission": "read"},
                    )
    if failure is not None:
        raise failure
    return {
        "id": user_id,
        "email": body.email,
        "display_name": body.display_name,
        "status": "active",
        "regions": [{"region_code": "CN_EAST", "permission": "read"}],
        "operator_permissions": [],
    }


@router.post("/api/v1/admin/identity/operators/{user_id}/disable")
async def disable_operator(
    user_id: uuid.UUID,
    request: Request,
    principal: dict[str, Any] = Depends(current_principal),
) -> dict[str, str]:
    failure: ApiError | None = None
    async with request.app.state.connect() as conn:
        async with conn.transaction():
            if not await operator_permission_allowed(
                conn, request, principal, "identity_admin"
            ):
                failure = ApiError(
                    403,
                    "OPERATOR_PERMISSION_REQUIRED",
                    "Operator permission is required",
                    details={"permission": "identity_admin"},
                )
            else:
                target = await conn.fetchrow(
                    "SELECT id::text AS id, status FROM identity.users "
                    "WHERE id = $1::uuid FOR UPDATE",
                    str(user_id),
                )
                has_operator_permission = await conn.fetchval(
                    "SELECT EXISTS (SELECT 1 FROM identity.operator_permissions "
                    "WHERE user_id = $1::uuid)",
                    str(user_id),
                )
                if target is None or not has_operator_permission:
                    failure = ApiError(404, "OPERATOR_NOT_FOUND", "Operator not found")
                elif target["status"] != "active":
                    failure = ApiError(409, "OPERATOR_NOT_ACTIVE", "Operator is not active")
                else:
                    await conn.execute(
                        "UPDATE identity.users SET status = 'disabled', "
                        "token_version = token_version + 1, updated_at = now() "
                        "WHERE id = $1::uuid",
                        str(user_id),
                    )
                    await _audit(
                        conn,
                        actor_type="user",
                        actor_id=principal["id"],
                        action="identity.operator.disabled",
                        resource_type="operator",
                        resource_id=str(user_id),
                        decision="allowed",
                        request=request,
                    )
    if failure is not None:
        async with request.app.state.connect() as conn:
            await _audit(
                conn,
                actor_type="user",
                actor_id=principal["id"],
                action="identity.operator.disable",
                resource_type="operator",
                resource_id=str(user_id),
                decision="denied",
                reason_code=failure.code,
                request=request,
            )
        raise failure
    return {"id": str(user_id), "status": "disabled"}


@router.post("/api/v1/operator-recovery")
async def recover_operator(
    body: OperatorRecoveryRequest, request: Request
) -> dict[str, str]:
    secret = body.recovery_secret.get_secret_value()
    digest = _secret_digest(secret)
    async with request.app.state.connect() as conn:
        state = await conn.fetchrow(
            "SELECT action, target_user_id::text AS target_user_id, secret_digest, "
            "expires_at, consumed_at, expires_at <= clock_timestamp() AS expired "
            "FROM identity.operator_recovery_state WHERE singleton = TRUE"
        )
        if state is None or not hmac.compare_digest(bytes(state["secret_digest"]), digest):
            raise await _recovery_rejection(
                request,
                status=403,
                code="RECOVERY_UNAVAILABLE",
                message="Operator recovery is unavailable",
            )
        if state["expired"]:
            raise await _recovery_rejection(
                request,
                status=410,
                code="RECOVERY_EXPIRED",
                message="Operator recovery has expired",
            )
        if state["consumed_at"] is not None:
            raise await _recovery_rejection(
                request,
                status=409,
                code="RECOVERY_CONSUMED",
                message="Operator recovery has already been consumed",
            )
        action = state["action"]
        target_user_id = state["target_user_id"]
        if action == "restore" and body.new_password is None:
            raise await _recovery_rejection(
                request,
                status=400,
                code="INVALID_REQUEST",
                message="A new password is required for recovery",
            )
        if action == "disable" and body.new_password is not None:
            raise await _recovery_rejection(
                request,
                status=400,
                code="INVALID_REQUEST",
                message="A password is not accepted for disable recovery",
            )

    password_hash = (
        await hash_password(body.new_password.get_secret_value())
        if body.new_password is not None
        else None
    )
    failure: ApiError | None = None
    async with request.app.state.connect() as conn:
        async with conn.transaction():
            locked = await conn.fetchrow(
                "SELECT action, target_user_id::text AS target_user_id, secret_digest, "
                "expires_at, consumed_at, expires_at <= clock_timestamp() AS expired "
                "FROM identity.operator_recovery_state WHERE singleton = TRUE FOR UPDATE"
            )
            if (
                locked is None
                or not hmac.compare_digest(bytes(locked["secret_digest"]), digest)
            ):
                failure = ApiError(403, "RECOVERY_UNAVAILABLE", "Operator recovery is unavailable")
            elif locked["expired"]:
                failure = ApiError(410, "RECOVERY_EXPIRED", "Operator recovery has expired")
            elif locked["consumed_at"] is not None:
                failure = ApiError(409, "RECOVERY_CONSUMED", "Operator recovery has already been consumed")
            else:
                action = locked["action"]
                target_user_id = locked["target_user_id"]
                target = await conn.fetchrow(
                    "SELECT status FROM identity.users WHERE id = $1::uuid FOR UPDATE",
                    target_user_id,
                )
                is_operator = await conn.fetchval(
                    "SELECT EXISTS (SELECT 1 FROM identity.operator_permissions "
                    "WHERE user_id = $1::uuid)",
                    target_user_id,
                )
                expected_status = "active" if action == "disable" else "disabled"
                if target is None or not is_operator:
                    failure = ApiError(404, "OPERATOR_NOT_FOUND", "Operator not found")
                elif target["status"] != expected_status:
                    failure = ApiError(409, "OPERATOR_STATE_CONFLICT", "Operator state changed")
                elif action == "restore" and password_hash is None:
                    failure = ApiError(400, "INVALID_REQUEST", "A new password is required for recovery")
                else:
                    if action == "disable":
                        await conn.execute(
                            "UPDATE identity.users SET status = 'disabled', "
                            "token_version = token_version + 1, updated_at = now() "
                            "WHERE id = $1::uuid",
                            target_user_id,
                        )
                    else:
                        await conn.execute(
                            "UPDATE identity.users SET status = 'active', password_hash = $2, "
                            "token_version = token_version + 1, updated_at = now() "
                            "WHERE id = $1::uuid",
                            target_user_id,
                            password_hash,
                        )
                    await _audit(
                        conn,
                        actor_type="recovery",
                        actor_id=target_user_id,
                        action=(
                            "identity.operator.disabled"
                            if action == "disable"
                            else "identity.operator.recovered"
                        ),
                        resource_type="operator",
                        resource_id=target_user_id,
                        decision="allowed",
                        request=request,
                        metadata={"recovery_action": action},
                    )
                    await conn.execute(
                        "UPDATE identity.operator_recovery_state "
                        "SET consumed_at = clock_timestamp(), consumed_by = $1::uuid "
                        "WHERE singleton = TRUE",
                        target_user_id,
                    )
            if failure is not None:
                await _audit(
                    conn,
                    actor_type="anonymous",
                    actor_id=None,
                    action="identity.operator.recovery",
                    resource_type="operator",
                    resource_id=(
                        locked["target_user_id"]
                        if locked is not None
                        else None
                    ),
                    decision="denied",
                    reason_code=failure.code,
                    request=request,
                    metadata=(
                        {"recovery_action": locked["action"]}
                        if locked is not None
                        else None
                    ),
                )
    if failure is not None:
        raise failure
    return {"id": target_user_id, "status": "disabled" if action == "disable" else "active"}
