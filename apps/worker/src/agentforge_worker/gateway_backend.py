"""PostgreSQL adapter for the harness ToolGateway protocol."""

from __future__ import annotations

import hashlib
import json
import uuid
from decimal import Decimal
from typing import Any

import asyncpg

from agentforge_harness.gateway import ToolContext, ToolDescriptor
from agentforge_runtime.store import RuntimeStore
from agentforge_core.observability import span


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _json_value(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


class PostgresToolGatewayBackend:
    """All gateway state transitions use short, fenced transactions."""

    def __init__(self, pool: asyncpg.Pool, store: RuntimeStore) -> None:
        self._pool = pool
        self._store = store

    async def resolve_tool(self, tool_key: str) -> ToolDescriptor | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id::text AS id, tool_key, version, input_schema,
                       output_schema, policy_json, execution_kind, is_read_only,
                       timeout_ms, status
                FROM registry.tool_versions
                WHERE tool_key = $1 AND status = 'active'
                ORDER BY created_at DESC, version DESC
                LIMIT 1
                """,
                tool_key,
            )
        if row is None:
            return None
        return ToolDescriptor(
            id=row["id"],
            tool_key=row["tool_key"],
            version=row["version"],
            input_schema=_json_value(row["input_schema"]),
            output_schema=_json_value(row["output_schema"]),
            policy_json=_json_value(row["policy_json"]),
            execution_kind=row["execution_kind"],
            is_read_only=row["is_read_only"],
            timeout_ms=row["timeout_ms"],
            status=row["status"],
        )

    async def binding_override(
        self, agent_version_id: str, tool_version_id: str
    ) -> dict[str, Any] | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT policy_override FROM registry.agent_tool_bindings "
                "WHERE agent_version_id = $1::uuid AND tool_version_id = $2::uuid",
                agent_version_id,
                tool_version_id,
            )
        return None if row is None else _json_value(row["policy_override"])

    async def has_live_region_grant(
        self, principal_id: str, region_code: str
    ) -> bool:
        async with self._pool.acquire() as conn:
            return bool(
                await conn.fetchval(
                    """
                    SELECT EXISTS (
                        SELECT 1 FROM identity.region_grants
                        WHERE user_id = $1::uuid AND region_code = $2
                          AND permission IN ('read', 'admin') AND revoked_at IS NULL
                    )
                    """,
                    principal_id,
                    region_code,
                )
            )

    async def lease_is_valid(self, context: ToolContext) -> bool:
        from agentforge_runtime.contracts import AttemptLease

        return await self._store.lease_is_valid(
            AttemptLease(
                attempt_id=context.attempt_id,
                task_id=context.task_id,
                attempt_no=context.fencing_epoch,
                worker_id="",
                lease_token=context.lease_token,
                fencing_epoch=context.fencing_epoch,
                trace_id=context.trace_id,
            )
        )

    async def accepted_call_count(self, task_id: str, tool_version_id: str) -> int:
        async with self._pool.acquire() as conn:
            return await conn.fetchval(
                "SELECT count(*) FROM harness.tool_invocations "
                "WHERE task_id = $1::uuid AND tool_version_id = $2::uuid "
                "AND status = 'succeeded'",
                task_id,
                tool_version_id,
            )

    async def find_invocation(
        self, task_id: str, invocation_key: str
    ) -> dict[str, Any] | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT invocation.request_hash, invocation.status,
                       invocation.result_json, invocation.result_hash,
                       evidence.id::text AS evidence_id
                FROM harness.tool_invocations AS invocation
                LEFT JOIN harness.evidences AS evidence
                  ON evidence.tool_invocation_id = invocation.id
                WHERE invocation.task_id = $1::uuid
                  AND invocation.invocation_key = $2
                """,
                task_id,
                invocation_key,
            )
        if row is None:
            return None
        return {
            "request_hash": row["request_hash"],
            "status": row["status"],
            "result_json": _json_value(row["result_json"]),
            "result_hash": row["result_hash"],
            "evidence_id": row["evidence_id"],
        }

    async def reject(
        self,
        context: ToolContext,
        descriptor: ToolDescriptor | None,
        invocation_key: str,
        request_hash: str,
        request_json: dict[str, Any],
        code: str,
        detail: str,
    ) -> None:
        with span(
            "tool.rejected",
            trace_id=context.trace_id,
            task_id=context.task_id,
            attempt_id=context.attempt_id,
        ):
            await self._reject(
                context, descriptor, invocation_key, request_hash, request_json, code, detail
            )

    async def _reject(
        self,
        context: ToolContext,
        descriptor: ToolDescriptor | None,
        invocation_key: str,
        request_hash: str,
        request_json: dict[str, Any],
        code: str,
        detail: str,
    ) -> None:
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                if descriptor is not None:
                    await conn.execute(
                        """
                        INSERT INTO harness.tool_invocations
                            (id, task_id, attempt_id, invocation_key,
                             tool_version_id, request_hash, request_json, status,
                             error_code, finished_at, trace_id)
                        VALUES ($1::uuid, $2::uuid, $3::uuid, $4, $5::uuid, $6,
                                $7::jsonb, 'failed', $8, now(), $9)
                        ON CONFLICT (task_id, invocation_key) DO UPDATE
                        SET status = 'failed', error_code = EXCLUDED.error_code,
                            finished_at = now(), trace_id = EXCLUDED.trace_id
                        WHERE harness.tool_invocations.status <> 'succeeded'
                        """,
                        str(uuid.uuid4()),
                        context.task_id,
                        context.attempt_id,
                        invocation_key,
                        descriptor.id,
                        request_hash,
                        _canonical(request_json),
                        code,
                        context.trace_id,
                    )
                await conn.execute(
                    """
                    INSERT INTO audit.audit_events
                        (actor_type, actor_id, action, resource_type, resource_id,
                         decision, reason_code, metadata_json, trace_id)
                    VALUES ('worker', $1::uuid, 'tool.invocation', 'tool', $2,
                            'denied', $3, $4::jsonb, $5)
                    """,
                    context.principal_id,
                    None if descriptor is None else descriptor.id,
                    code,
                    _canonical({
                        "tool_key": None if descriptor is None else descriptor.tool_key,
                        "invocation_key": invocation_key,
                        "step_key": context.step_key,
                        "request_hash": request_hash,
                        "detail": detail[:1000],
                        "attempt_id": context.attempt_id,
                        "fencing_epoch": context.fencing_epoch,
                    }),
                    context.trace_id,
                )

    async def accept(
        self,
        context: ToolContext,
        descriptor: ToolDescriptor,
        invocation_key: str,
        request_hash: str,
        request_json: dict[str, Any],
        result: dict[str, Any],
        query_plan: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        result_json = _canonical(result)
        query_plan_json = _canonical(
            query_plan if query_plan is not None else request_json
        )
        result_hash = _hash(result)
        with span(
            "tool.accepted",
            trace_id=context.trace_id,
            task_id=context.task_id,
            attempt_id=context.attempt_id,
        ):
            return await self._accept(
                context,
                descriptor,
                invocation_key,
                request_hash,
                request_json,
                result_json,
                query_plan_json,
                result_hash,
                result,
            )

    async def _accept(
        self,
        context: ToolContext,
        descriptor: ToolDescriptor,
        invocation_key: str,
        request_hash: str,
        request_json: dict[str, Any],
        result_json: str,
        query_plan_json: str,
        result_hash: str,
        result: dict[str, Any],
    ) -> dict[str, Any] | None:
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                task = await conn.fetchrow(
                    """
                    SELECT status, cancel_requested_at, trace_id,
                           (deadline_at IS NOT NULL AND deadline_at <= now()) AS expired
                    FROM runtime.tasks WHERE id = $1::uuid FOR UPDATE
                    """,
                    context.task_id,
                )
                attempt = await conn.fetchrow(
                    """
                    SELECT 1 FROM runtime.task_attempts
                    WHERE id = $1::uuid AND task_id = $2::uuid
                      AND lease_token = $3::uuid AND fencing_epoch = $4
                      AND status = 'running' AND lease_expires_at > now()
                    """,
                    context.attempt_id,
                    context.task_id,
                    context.lease_token,
                    context.fencing_epoch,
                )
                if (
                    task is None
                    or attempt is None
                    or task["status"] != "running"
                    or task["cancel_requested_at"] is not None
                    or task["expired"]
                ):
                    return None

                row = await conn.fetchrow(
                    """
                    INSERT INTO harness.tool_invocations
                        (id, task_id, attempt_id, invocation_key, tool_version_id,
                         request_hash, request_json, status, result_json,
                         result_hash, finished_at, trace_id)
                    VALUES ($1::uuid, $2::uuid, $3::uuid, $4, $5::uuid, $6,
                            $7::jsonb, 'succeeded', $8::jsonb, $9, now(), $10)
                    ON CONFLICT (task_id, invocation_key) DO UPDATE
                    SET status = 'succeeded', result_json = EXCLUDED.result_json,
                        result_hash = EXCLUDED.result_hash, error_code = NULL,
                        finished_at = now()
                    WHERE harness.tool_invocations.status <> 'succeeded'
                    RETURNING id::text AS id
                    """,
                    str(uuid.uuid4()),
                    context.task_id,
                    context.attempt_id,
                    invocation_key,
                    descriptor.id,
                    request_hash,
                    _canonical(request_json),
                    result_json,
                    result_hash,
                    context.trace_id,
                )
                if row is None:
                    existing = await conn.fetchrow(
                        """
                        SELECT invocation.id::text AS id,
                               invocation.result_json,
                               evidence.id::text AS evidence_id
                        FROM harness.tool_invocations AS invocation
                        LEFT JOIN harness.evidences AS evidence
                          ON evidence.tool_invocation_id = invocation.id
                        WHERE invocation.task_id = $1::uuid
                          AND invocation.invocation_key = $2
                        """,
                        context.task_id,
                        invocation_key,
                    )
                    return {
                        "result": (
                            result
                            if existing is None
                            else _json_value(existing["result_json"])
                        ),
                        "evidence_id": None if existing is None else existing["evidence_id"],
                        "reused": True,
                    }

                invocation_id = row["id"]
                evidence = await conn.fetchrow(
                    """
                    INSERT INTO harness.evidences
                        (id, task_id, attempt_id, tool_invocation_id, metric_key,
                         metric_version, unit, current_value, previous_value,
                         absolute_change, change_rate, query_plan_json,
                         result_json, result_set_hash, row_count, data_snapshot_id)
                    VALUES ($1::uuid, $2::uuid, $3::uuid, $4::uuid, $5, $6, $7,
                            $8, $9, $10, $11, $12::jsonb, $13::jsonb, $14, $15, $16)
                    ON CONFLICT (tool_invocation_id)
                        WHERE tool_invocation_id IS NOT NULL DO NOTHING
                    RETURNING id::text AS evidence_id
                    """,
                    str(uuid.uuid4()),
                    context.task_id,
                    context.attempt_id,
                    invocation_id,
                    result["metric"],
                    result["metric_version"],
                    result["unit"],
                    Decimal(str(result["current_value"])),
                    Decimal(str(result["previous_value"])),
                    Decimal(str(result["absolute_change"])),
                    (
                        None
                        if result.get("change_rate") is None
                        else Decimal(str(result["change_rate"]))
                    ),
                    query_plan_json,
                    result_json,
                    result_hash,
                    result["row_count"],
                    context.task_snapshot_id,
                )
                if evidence is None:
                    evidence = await conn.fetchrow(
                        "SELECT id::text AS evidence_id FROM harness.evidences "
                        "WHERE tool_invocation_id = $1::uuid",
                        invocation_id,
                    )
                await conn.execute(
                    """
                    INSERT INTO audit.audit_events
                        (actor_type, actor_id, action, resource_type, resource_id,
                         decision, metadata_json, trace_id)
                    VALUES ('worker', $1::uuid, 'tool.invocation', 'tool', $2,
                            'allowed', $3::jsonb, $4)
                    """,
                    context.principal_id,
                    descriptor.id,
                    _canonical({
                        "tool_key": descriptor.tool_key,
                        "invocation_key": invocation_key,
                        "step_key": context.step_key,
                        "request_hash": request_hash,
                        "evidence_id": evidence["evidence_id"],
                        "attempt_id": context.attempt_id,
                        "fencing_epoch": context.fencing_epoch,
                    }),
                    context.trace_id,
                )
                return {
                    "result": result,
                    "evidence_id": evidence["evidence_id"],
                    "reused": False,
                }
