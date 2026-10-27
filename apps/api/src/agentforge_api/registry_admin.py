"""Authenticated Registry write API for trusted Operators."""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any, Literal
from urllib.parse import urlsplit

import asyncpg
from agentforge_core.registry import (
    RegistryError,
    _canonical,
    _config_hash,
    _decode_jsonb,
    _reject_embedded_credentials,
    _validate_agent,
)
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from agentforge_api.auth import ApiError, current_principal, operator_permission_allowed

router = APIRouter(prefix="/api/v1/admin/registry", tags=["registry-admin"])


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ModelProfileCreate(_StrictModel):
    profile_key: str = Field(min_length=1, max_length=128)
    provider: str = Field(min_length=1, max_length=64)
    model_name: str = Field(min_length=1, max_length=128)
    endpoint_alias: str = Field(min_length=1, max_length=128)
    credential_ref: str = Field(min_length=1, max_length=512)
    timeout_ms: int = Field(gt=0, le=300_000)
    max_output_tokens: int = Field(gt=0, le=100_000)
    config_json: dict[str, Any] = Field(default_factory=dict)

    @field_validator("credential_ref")
    @classmethod
    def _credential_ref_is_secret_uri(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "secret"
            or not parsed.netloc
            or not parsed.path.strip("/")
            or parsed.query
            or parsed.fragment
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError("credential_ref must be a secret:// reference")
        return value


class ToolCreate(_StrictModel):
    tool_key: str = Field(min_length=1, max_length=128)
    version: str = Field(min_length=1, max_length=64)
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    policy_json: dict[str, Any] = Field(default_factory=dict)
    execution_kind: Literal["internal", "mcp"]
    timeout_ms: int = Field(gt=0, le=300_000)


class AgentCreate(_StrictModel):
    agent_key: str = Field(min_length=1, max_length=128)
    version: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=256)
    graph_key: str = Field(min_length=1, max_length=128)
    model_profile: str = Field(min_length=1, max_length=128)
    config_json: dict[str, Any] = Field(default_factory=dict)


class ToolBindingCreate(_StrictModel):
    policy_override: dict[str, Any] = Field(default_factory=dict)

async def _audit(
    conn: asyncpg.Connection,
    request: Request,
    principal: dict[str, Any],
    *,
    action: str,
    resource_type: str,
    resource_id: str | None,
    metadata: dict[str, Any] | None = None,
) -> None:
    await conn.execute(
        """
        INSERT INTO audit.audit_events
            (actor_type, actor_id, action, resource_type, resource_id,
             decision, metadata_json, trace_id)
        VALUES ('user', $1::uuid, $2, $3, $4, 'allowed', $5::jsonb, $6)
        """,
        principal["id"],
        action,
        resource_type,
        resource_id,
        json.dumps(metadata or {}, sort_keys=True, separators=(",", ":")),
        request.state.request_id,
    )


async def _audit_duplicate(
    request: Request,
    principal: dict[str, Any],
    *,
    reason_code: str = "REGISTRY_CONFLICT",
) -> None:
    async with request.app.state.connect() as conn:
        await conn.execute(
            """
            INSERT INTO audit.audit_events
                (actor_type, actor_id, action, resource_type, decision,
                 reason_code, metadata_json, trace_id)
            VALUES ('user', $1::uuid, 'registry.write', 'registry', 'denied',
                    $2, '{}'::jsonb, $3)
            """,
            principal["id"],
            reason_code,
            request.state.request_id,
        )


def _registry_error(error: Exception) -> ApiError:
    if isinstance(error, RegistryError):
        return ApiError(400, "INVALID_REQUEST", str(error))
    if isinstance(error, asyncpg.CheckViolationError) and "only tighten" in str(error).lower():
        return ApiError(
            400,
            "INVALID_POLICY_OVERRIDE",
            "Policy override may only tighten the base tool policy",
        )
    return ApiError(409, "REGISTRY_CONFLICT", "Registry resource conflicts with current state")


async def _authorize(
    conn: asyncpg.Connection,
    request: Request,
    principal: dict[str, Any],
) -> bool:
    return await operator_permission_allowed(
        conn, request, principal, "registry_operator"
    )


@router.post("/model-profiles", status_code=201)
async def create_model_profile(
    body: ModelProfileCreate,
    request: Request,
    principal: dict[str, Any] = Depends(current_principal),
) -> dict[str, str]:
    profile_id = str(uuid.uuid4())
    failure: ApiError | None = None
    try:
        async with request.app.state.connect() as conn:
            async with conn.transaction():
                if not await _authorize(conn, request, principal):
                    failure = ApiError(403, "OPERATOR_PERMISSION_REQUIRED", "Operator permission is required", details={"permission": "registry_operator"})
                else:
                    _reject_embedded_credentials(body.config_json)
                    await conn.execute(
                        """
                        INSERT INTO registry.model_profiles
                            (id, profile_key, provider, model_name, endpoint_alias,
                             credential_ref, timeout_ms, max_output_tokens, config_json)
                        VALUES ($1::uuid, $2, $3, $4, $5, $6, $7, $8, $9::jsonb)
                        """,
                        profile_id,
                        body.profile_key,
                        body.provider,
                        body.model_name,
                        body.endpoint_alias,
                        body.credential_ref,
                        body.timeout_ms,
                        body.max_output_tokens,
                        _canonical(body.config_json),
                    )
                    await _audit(
                        conn,
                        request,
                        principal,
                        action="registry.model_profile.created",
                        resource_type="model_profile",
                        resource_id=profile_id,
                        metadata={"profile_key": body.profile_key, "provider": body.provider},
                    )
    except RegistryError as error:
        await _audit_duplicate(
            request, principal, reason_code="REGISTRY_VALIDATION_FAILED"
        )
        failure = _registry_error(error)
    except (asyncpg.UniqueViolationError, asyncpg.CheckViolationError) as error:
        await _audit_duplicate(request, principal)
        failure = _registry_error(error)
    if failure is not None:
        raise failure
    return {"id": profile_id, "profile_key": body.profile_key}


@router.post("/tools", status_code=201)
async def create_tool(
    body: ToolCreate,
    request: Request,
    principal: dict[str, Any] = Depends(current_principal),
) -> dict[str, str]:
    tool_id = str(uuid.uuid4())
    failure: ApiError | None = None
    async with request.app.state.connect() as conn:
        try:
            async with conn.transaction():
                if not await _authorize(conn, request, principal):
                    failure = ApiError(403, "OPERATOR_PERMISSION_REQUIRED", "Operator permission is required", details={"permission": "registry_operator"})
                else:
                    await conn.execute(
                        """
                        INSERT INTO registry.tool_versions
                            (id, tool_key, version, input_schema, output_schema,
                             policy_json, execution_kind, timeout_ms)
                        VALUES ($1::uuid, $2, $3, $4::jsonb, $5::jsonb, $6::jsonb, $7, $8)
                        """,
                        tool_id,
                        body.tool_key,
                        body.version,
                        _canonical(body.input_schema),
                        _canonical(body.output_schema),
                        _canonical(body.policy_json),
                        body.execution_kind,
                        body.timeout_ms,
                    )
                    await _audit(
                        conn,
                        request,
                        principal,
                        action="registry.tool_version.created",
                        resource_type="tool_version",
                        resource_id=tool_id,
                        metadata={"tool_key": body.tool_key, "version": body.version},
                    )
        except (asyncpg.UniqueViolationError, asyncpg.CheckViolationError) as error:
            await _audit_duplicate(request, principal)
            failure = _registry_error(error)
    if failure is not None:
        raise failure
    return {"id": tool_id, "tool_key": body.tool_key, "version": body.version}


@router.post("/agents", status_code=201)
async def create_agent(
    body: AgentCreate,
    request: Request,
    principal: dict[str, Any] = Depends(current_principal),
) -> dict[str, str]:
    agent_id = str(uuid.uuid4())
    config_hash = hashlib.sha256(_canonical(body.config_json).encode("utf-8")).hexdigest()
    failure: ApiError | None = None
    async with request.app.state.connect() as conn:
        try:
            async with conn.transaction():
                if not await _authorize(conn, request, principal):
                    failure = ApiError(403, "OPERATOR_PERMISSION_REQUIRED", "Operator permission is required", details={"permission": "registry_operator"})
                else:
                    profile_id = await conn.fetchval(
                        "SELECT id FROM registry.model_profiles WHERE profile_key = $1",
                        body.model_profile,
                    )
                    if profile_id is None:
                        failure = ApiError(404, "MODEL_PROFILE_NOT_FOUND", "Model profile not found")
                    else:
                        _reject_embedded_credentials(body.config_json)
                        await conn.execute(
                            """
                            INSERT INTO registry.agent_versions
                                (id, agent_key, version, name, graph_key, model_profile_id,
                                 model_config_snapshot, config_json, config_hash, status, created_by)
                            VALUES ($1::uuid, $2, $3, $4, $5, $6::uuid,
                                    '{}'::jsonb, $7::jsonb, $8, 'draft', $9::uuid)
                            """,
                            agent_id,
                            body.agent_key,
                            body.version,
                            body.name,
                            body.graph_key,
                            str(profile_id),
                            _canonical(body.config_json),
                            config_hash,
                            principal["id"],
                        )
                        await _audit(
                            conn,
                            request,
                            principal,
                            action="registry.agent_version.created",
                            resource_type="agent_version",
                            resource_id=agent_id,
                            metadata={"agent_key": body.agent_key, "version": body.version},
                        )
        except RegistryError as error:
            await _audit_duplicate(
                request, principal, reason_code="REGISTRY_VALIDATION_FAILED"
            )
            failure = _registry_error(error)
        except (asyncpg.UniqueViolationError, asyncpg.CheckViolationError) as error:
            await _audit_duplicate(request, principal)
            failure = _registry_error(error)
    if failure is not None:
        raise failure
    return {"id": agent_id, "agent_key": body.agent_key, "version": body.version, "status": "draft"}


@router.put("/agents/{version_id}/tools/{tool_id}")
async def bind_tool(
    version_id: uuid.UUID,
    tool_id: uuid.UUID,
    body: ToolBindingCreate,
    request: Request,
    principal: dict[str, Any] = Depends(current_principal),
) -> dict[str, Any]:
    failure: ApiError | None = None
    async with request.app.state.connect() as conn:
        try:
            async with conn.transaction():
                if not await _authorize(conn, request, principal):
                    failure = ApiError(403, "OPERATOR_PERMISSION_REQUIRED", "Operator permission is required", details={"permission": "registry_operator"})
                else:
                    await conn.execute(
                        """
                        INSERT INTO registry.agent_tool_bindings
                            (agent_version_id, tool_version_id, policy_override)
                        VALUES ($1::uuid, $2::uuid, $3::jsonb)
                        """,
                        str(version_id),
                        str(tool_id),
                        _canonical(body.policy_override),
                    )
                    await _audit(
                        conn,
                        request,
                        principal,
                        action="registry.agent_tool.bound",
                        resource_type="agent_version",
                        resource_id=str(version_id),
                        metadata={"tool_version_id": str(tool_id)},
                    )
        except (
            asyncpg.UniqueViolationError,
            asyncpg.CheckViolationError,
            asyncpg.ForeignKeyViolationError,
        ) as error:
            await _audit_duplicate(request, principal)
            failure = _registry_error(error)
    if failure is not None:
        raise failure
    return {
        "agent_version_id": str(version_id),
        "tool_version_id": str(tool_id),
        "policy_override": body.policy_override,
    }


@router.post("/agents/{version_id}/publish")
async def publish_agent(
    version_id: uuid.UUID,
    request: Request,
    principal: dict[str, Any] = Depends(current_principal),
) -> dict[str, Any]:
    failure: ApiError | None = None
    published: dict[str, Any] | None = None
    async with request.app.state.connect() as conn:
        try:
            async with conn.transaction():
                if not await _authorize(conn, request, principal):
                    failure = ApiError(403, "OPERATOR_PERMISSION_REQUIRED", "Operator permission is required", details={"permission": "registry_operator"})
                else:
                    await conn.execute(
                        "SELECT set_config('agentforge.actor_id', $1, true)",
                        principal["id"],
                    )
                    await conn.fetchval(
                        "SELECT id FROM registry.agent_versions WHERE id = $1::uuid FOR UPDATE",
                        str(version_id),
                    )
                    details, errors = await _validate_agent(conn, version_id, lock=True)
                    if errors:
                        failure = ApiError(
                            409,
                            "AGENT_VALIDATION_FAILED",
                            "Agent version is not valid for publication",
                            details={"errors": errors},
                        )
                    else:
                        profile = details["profile"]
                        snapshot = {
                            "provider": profile["provider"],
                            "model_name": profile["model_name"],
                            "endpoint_alias": profile["endpoint_alias"],
                            "credential_ref": profile["credential_ref"],
                            "timeout_ms": profile["timeout_ms"],
                            "max_output_tokens": profile["max_output_tokens"],
                            "config_json": profile["config_json"],
                        }
                        bindings = [
                            {
                                "tool_version_id": str(row["tool_version_id"]),
                                "policy_override": row["policy_override"],
                            }
                            for row in details["bindings"]
                        ]
                        config_json = await conn.fetchval(
                            "SELECT config_json FROM registry.agent_versions WHERE id = $1::uuid",
                            str(version_id),
                        )
                        config_json = _decode_jsonb(config_json)
                        config_hash = _config_hash(
                            agent_key=details["agent"]["agent_key"],
                            version=details["agent"]["version"],
                            graph_key=details["agent"]["graph_key"],
                            config_json=config_json,
                            model_config_snapshot=snapshot,
                            bindings=bindings,
                        )
                        await conn.execute(
                            "UPDATE registry.agent_versions "
                            "SET model_config_snapshot = $2::jsonb, config_hash = $3, "
                            "status = 'published', published_at = now() "
                            "WHERE id = $1::uuid",
                            str(version_id),
                            _canonical(snapshot),
                            config_hash,
                        )
                        row = await conn.fetchrow(
                            "SELECT id::text AS id, agent_key, version, status, "
                            "model_config_snapshot, config_hash, published_at "
                            "FROM registry.agent_versions WHERE id = $1::uuid",
                            str(version_id),
                        )
                        published = dict(row)
                        published["model_config_snapshot"] = _decode_jsonb(
                            published["model_config_snapshot"]
                        )
        except RegistryError:
            failure = ApiError(404, "AGENT_VERSION_NOT_FOUND", "Agent version not found")
        except (asyncpg.CheckViolationError, asyncpg.ForeignKeyViolationError) as error:
            failure = _registry_error(error)
    if failure is not None:
        raise failure
    if published is None:
        raise ApiError(404, "AGENT_VERSION_NOT_FOUND", "Agent version not found")
    return published


@router.post("/agents/{version_id}/retire")
async def retire_agent(
    version_id: uuid.UUID,
    request: Request,
    principal: dict[str, Any] = Depends(current_principal),
) -> dict[str, str]:
    failure: ApiError | None = None
    async with request.app.state.connect() as conn:
        async with conn.transaction():
            if not await _authorize(conn, request, principal):
                failure = ApiError(
                    403,
                    "OPERATOR_PERMISSION_REQUIRED",
                    "Operator permission is required",
                    details={"permission": "registry_operator"},
                )
            else:
                await conn.execute(
                    "SELECT set_config('agentforge.actor_id', $1, true)",
                    principal["id"],
                )
                result = await conn.execute(
                    "UPDATE registry.agent_versions SET status = 'retired' "
                    "WHERE id = $1::uuid AND status = 'published'",
                    str(version_id),
                )
                if result != "UPDATE 1":
                    failure = ApiError(
                        409,
                        "AGENT_VERSION_NOT_PUBLISHED",
                        "Published Agent version not found",
                    )
    if failure is not None:
        raise failure
    return {"id": str(version_id), "status": "retired"}


@router.get("/tool-versions/{tool_id}")
async def get_tool_version(
    tool_id: uuid.UUID,
    request: Request,
    principal: dict[str, Any] = Depends(current_principal),
) -> dict[str, Any]:
    async with request.app.state.connect() as conn:
        async with conn.transaction():
            if not await _authorize(conn, request, principal):
                denied = True
                row = None
            else:
                denied = False
                row = await conn.fetchrow(
                    "SELECT id::text AS id, tool_key, version, status "
                    "FROM registry.tool_versions WHERE id = $1::uuid",
                    str(tool_id),
                )
    if denied:
        raise ApiError(403, "OPERATOR_PERMISSION_REQUIRED", "Operator permission is required", details={"permission": "registry_operator"})
    if row is None:
        raise ApiError(404, "TOOL_VERSION_NOT_FOUND", "Tool version not found")
    return dict(row)
