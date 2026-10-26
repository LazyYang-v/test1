"""PostgreSQL-backed task claims, leases, step replay, and terminal commits.

Every lifecycle write validates (attempt_id, lease_token, fencing_epoch) using
database time inside the same short transaction as the write. No transaction or
Task row lock ever spans model or tool execution.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any

import asyncpg

from agentforge_runtime.contracts import AttemptLease, StepRecord
from agentforge_runtime.errors import LeaseLostError, TaskNotRunningError
from agentforge_core.faults import (
    FaultInjector,
    FaultPoint,
    InjectedCrash,
    NOOP_FAULT_INJECTOR,
)
from agentforge_runtime.recovery import validate_recovery_records

CANCEL_REQUEST_EVENT = "task.cancel.requested"
TERMINAL_TASK_STATUSES = frozenset({"completed", "failed", "cancelled"})
ACTIVE_TASK_STATUSES = frozenset(
    {"queued", "running", "retry_wait", "recovering", "cancelling"}
)


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _json_value(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def _row_to_lease(row: asyncpg.Record) -> AttemptLease:
    return AttemptLease(
        attempt_id=str(row["attempt_id"]),
        task_id=str(row["task_id"]),
        attempt_no=row["attempt_no"],
        worker_id=row["worker_id"],
        lease_token=str(row["lease_token"]),
        fencing_epoch=row["fencing_epoch"],
        lease_expires_at=row["lease_expires_at"],
    )


class RuntimeStore:
    def __init__(
        self,
        pool: asyncpg.Pool,
        *,
        worker_id: str,
        lease_seconds: float = 30.0,
        fault_injector: FaultInjector | None = None,
    ) -> None:
        self.pool = pool
        self.worker_id = worker_id
        self.lease_seconds = lease_seconds
        self.fault_injector = fault_injector or NOOP_FAULT_INJECTOR

    async def close(self) -> None:
        await self.pool.close()

    # -- claim and lease -------------------------------------------------

    async def claim(self) -> AttemptLease | None:
        """Claim one queued/retry-due Task with FOR UPDATE SKIP LOCKED.

        A ``retry_wait`` Task is claimable only once ``next_retry_at`` has
        passed. The Task row lock, Attempt insert, and started event all commit
        together; the conditional UPDATE re-checks cancellation after the
        per-Task event lock, so a cancel request that lands during the claim
        wins.
        """
        attempt_id = str(uuid.uuid4())
        lease_token = str(uuid.uuid4())
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                candidate = await conn.fetchrow(
                    """
                    SELECT task.id::text AS id
                    FROM runtime.tasks AS task
                    WHERE (
                            task.status = 'queued'
                            OR (task.status = 'retry_wait'
                                AND task.next_retry_at <= now())
                          )
                      AND task.cancel_requested_at IS NULL
                      AND NOT EXISTS (
                          SELECT 1 FROM runtime.task_events AS event
                          WHERE event.task_id = task.id
                            AND event.event_type = $1
                      )
                      AND (task.deadline_at IS NULL OR task.deadline_at > now())
                      AND task.current_attempt_no < task.max_attempts
                    ORDER BY task.priority DESC, task.created_at, task.id
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                    """,
                    CANCEL_REQUEST_EVENT,
                )
                if candidate is None:
                    return None
                await self._lock_events(conn, candidate["id"])
                task = await conn.fetchrow(
                    """
                    UPDATE runtime.tasks AS task
                    SET status = 'running',
                        current_attempt_no = task.current_attempt_no + 1,
                        next_retry_at = NULL,
                        version = task.version + 1,
                        updated_at = now()
                    WHERE task.id = $1::uuid
                      AND (
                            task.status = 'queued'
                            OR (task.status = 'retry_wait'
                                AND task.next_retry_at <= now())
                          )
                      AND task.cancel_requested_at IS NULL
                      AND NOT EXISTS (
                          SELECT 1 FROM runtime.task_events AS event
                          WHERE event.task_id = task.id
                            AND event.event_type = $2
                      )
                      AND (task.deadline_at IS NULL OR task.deadline_at > now())
                      AND task.current_attempt_no < task.max_attempts
                    RETURNING task.id, task.current_attempt_no
                    """,
                    candidate["id"],
                    CANCEL_REQUEST_EVENT,
                )
                if task is None:
                    return None
                attempt_no = task["current_attempt_no"]
                row = await conn.fetchrow(
                    """
                    INSERT INTO runtime.task_attempts
                        (id, task_id, attempt_no, status, worker_id, lease_token,
                         fencing_epoch, lease_expires_at, heartbeat_at)
                    VALUES ($1::uuid, $2::uuid, $3::integer, 'running', $4,
                            $5::uuid, $3::bigint,
                            now() + ($6::double precision * interval '1 second'),
                            now())
                    RETURNING id::text AS attempt_id, task_id::text AS task_id,
                              attempt_no, worker_id, lease_token, fencing_epoch,
                              lease_expires_at
                    """,
                    attempt_id,
                    str(task["id"]),
                    attempt_no,
                    self.worker_id,
                    lease_token,
                    self.lease_seconds,
                )
                await self._append_event(
                    conn,
                    task_id=str(task["id"]),
                    attempt_id=attempt_id,
                    event_type="task.attempt.started",
                    payload={
                        "attempt_no": attempt_no,
                        "worker_id": self.worker_id,
                        "fencing_epoch": attempt_no,
                    },
                )
                lease = _row_to_lease(row)
        try:
            await self.fault_injector.checkpoint(
                FaultPoint.ATTEMPT_CLAIMED,
                {"task_id": lease.task_id, "attempt_id": lease.attempt_id},
            )
        except InjectedCrash:
            raise
        return lease

    async def heartbeat(self, lease: AttemptLease) -> bool:
        """Renew the lease by a fixed interval using database time.

        A live ``cancelling`` Task keeps its lease renewable so the owning
        worker can finish the fenced cancellation transition. Any other
        non-running state stops renewal and lets recovery take over.
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE runtime.task_attempts AS attempt
                SET lease_expires_at = now()
                        + ($5::double precision * interval '1 second'),
                    heartbeat_at = now()
                WHERE attempt.id = $1::uuid
                  AND attempt.task_id = $2::uuid
                  AND attempt.lease_token = $3::uuid
                  AND attempt.fencing_epoch = $4
                  AND attempt.status = 'running'
                  AND attempt.lease_expires_at > now()
                  AND EXISTS (
                      SELECT 1 FROM runtime.tasks AS task
                      WHERE task.id = attempt.task_id
                        AND (
                            (task.status = 'running'
                             AND task.cancel_requested_at IS NULL)
                            OR task.status = 'cancelling'
                        )
                  )
                RETURNING attempt.heartbeat_at
                """,
                lease.attempt_id,
                lease.task_id,
                lease.lease_token,
                lease.fencing_epoch,
                self.lease_seconds,
            )
        return row is not None

    async def lease_is_valid(self, lease: AttemptLease) -> bool:
        async with self.pool.acquire() as conn:
            return bool(
                await conn.fetchval(
                    """
                    SELECT EXISTS (
                        SELECT 1
                        FROM runtime.task_attempts AS attempt
                        JOIN runtime.tasks AS task ON task.id = attempt.task_id
                        WHERE attempt.id = $1::uuid
                          AND attempt.task_id = $2::uuid
                          AND attempt.lease_token = $3::uuid
                          AND attempt.fencing_epoch = $4
                          AND attempt.status = 'running'
                          AND attempt.lease_expires_at > now()
                          AND task.status = 'running'
                          AND task.cancel_requested_at IS NULL
                          AND (task.deadline_at IS NULL OR task.deadline_at > now())
                          AND NOT EXISTS (
                              SELECT 1 FROM runtime.task_events AS event
                              WHERE event.task_id = task.id
                                AND event.event_type = $5
                          )
                    )
                    """,
                    lease.attempt_id,
                    lease.task_id,
                    lease.lease_token,
                    lease.fencing_epoch,
                    CANCEL_REQUEST_EVENT,
                )
            )

    async def stop_reason(self, lease: AttemptLease) -> str | None:
        """Return why the current attempt must stop, or ``None`` while live.

        This is a read-only boundary check used before every model/tool call.
        It deliberately does not take a Task lock.
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT task.status, task.cancel_requested_at,
                       (task.deadline_at IS NOT NULL
                        AND task.deadline_at <= now()) AS expired,
                       EXISTS (
                           SELECT 1 FROM runtime.task_events AS event
                           WHERE event.task_id = task.id
                             AND event.event_type = $5
                       ) AS cancel_event,
                       EXISTS (
                           SELECT 1 FROM runtime.task_attempts AS attempt
                           WHERE attempt.id = $1::uuid
                             AND attempt.task_id = task.id
                             AND attempt.lease_token = $3::uuid
                             AND attempt.fencing_epoch = $4
                             AND attempt.status = 'running'
                             AND attempt.lease_expires_at > now()
                       ) AS lease_alive
                FROM runtime.tasks AS task
                WHERE task.id = $2::uuid
                """,
                lease.attempt_id,
                lease.task_id,
                lease.lease_token,
                lease.fencing_epoch,
                CANCEL_REQUEST_EVENT,
            )
        if row is None or not row["lease_alive"]:
            return "LEASE_LOST"
        # A committed cancellation request wins races with deadline expiry.
        if (
            row["cancel_requested_at"] is not None
            or row["cancel_event"]
            or row["status"] == "cancelling"
        ):
            return "CANCELLED"
        if row["expired"]:
            return "DEADLINE_EXCEEDED"
        if row["status"] != "running":
            return "CANCELLED"
        return None

    # -- reads -----------------------------------------------------------

    async def load_task(self, task_id: str) -> dict[str, Any]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id::text AS task_id, owner_user_id::text AS owner_user_id,
                       agent_version_id::text AS agent_version_id, task_type,
                       status, input_json, input_hash, region_scope_json,
                       authorization_snapshot, data_snapshot_id, priority,
                       current_attempt_no, max_attempts, deadline_at, created_at
                FROM runtime.tasks WHERE id = $1::uuid
                """,
                task_id,
            )
        if row is None:
            raise LookupError(f"task {task_id} not found")
        result = dict(row)
        for field in ("input_json", "region_scope_json", "authorization_snapshot"):
            result[field] = _json_value(result[field])
        result["data_snapshot_id"] = result["data_snapshot_id"].strip()
        return result

    async def load_agent(self, agent_version_id: str) -> dict[str, Any]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT agent.agent_key, agent.version, agent.graph_key,
                       agent.model_config_snapshot, agent.config_json,
                       profile.provider, profile.model_name,
                       profile.timeout_ms, profile.max_output_tokens
                FROM registry.agent_versions AS agent
                JOIN registry.model_profiles AS profile
                  ON profile.id = agent.model_profile_id
                WHERE agent.id = $1::uuid
                """,
                agent_version_id,
            )
        if row is None:
            raise LookupError(f"agent version {agent_version_id} not found")
        result = dict(row)
        result["model_config_snapshot"] = _json_value(result["model_config_snapshot"])
        result["config_json"] = _json_value(result["config_json"])

        # Runtime behavior comes from the immutable publication snapshot. The
        # mutable profile row is only a fallback for legacy/draft rows whose
        # snapshot predates the frozen provider/timeout fields.
        snapshot = result["model_config_snapshot"]
        if not isinstance(snapshot, dict):
            snapshot = {}
        provider = snapshot.get("provider")
        model_name = snapshot.get("model_name") or snapshot.get("model")
        timeout_ms = snapshot.get("timeout_ms")
        max_output_tokens = snapshot.get("max_output_tokens")
        result["provider"] = (
            provider if isinstance(provider, str) and provider else result["provider"]
        )
        result["model_name"] = (
            model_name
            if isinstance(model_name, str) and model_name
            else result["model_name"]
        )
        result["timeout_ms"] = (
            timeout_ms
            if isinstance(timeout_ms, int) and not isinstance(timeout_ms, bool)
            and timeout_ms > 0
            else result["timeout_ms"]
        )
        result["max_output_tokens"] = (
            max_output_tokens
            if isinstance(max_output_tokens, int)
            and not isinstance(max_output_tokens, bool)
            and max_output_tokens > 0
            else result["max_output_tokens"]
        )
        return result

    async def get_confirmed_step(self, task_id: str, step_key: str) -> StepRecord | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT step_key, graph_node_key, status, result_json, result_hash,
                       owning_attempt_id::text AS owning_attempt_id, fencing_epoch
                FROM runtime.step_executions
                WHERE task_id = $1::uuid AND step_key = $2
                """,
                task_id,
                step_key,
            )
        if row is None or row["status"] != "confirmed":
            return None
        return StepRecord(
            step_key=row["step_key"],
            graph_node_key=row["graph_node_key"],
            status=row["status"],
            result=_json_value(row["result_json"]),
            result_hash=row["result_hash"],
            owning_attempt_id=row["owning_attempt_id"],
            fencing_epoch=row["fencing_epoch"],
        )

    async def evidence_owner(self, evidence_id: str) -> str | None:
        """Used only to make a foreign-evidence rejection explicit."""
        async with self.pool.acquire() as conn:
            return await conn.fetchval(
                "SELECT task_id::text FROM harness.evidences WHERE id = $1::uuid",
                evidence_id,
            )

    async def load_evidences(self, task_id: str) -> list[dict[str, Any]]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id::text AS evidence_id, task_id::text AS task_id,
                       data_snapshot_id, metric_key, metric_version, unit,
                       current_value, previous_value, absolute_change, change_rate,
                       row_count, result_json, query_plan_json, created_at
                FROM harness.evidences
                WHERE task_id = $1::uuid
                ORDER BY created_at, id
                """,
                task_id,
            )
        return [
            {
                **dict(row),
                "data_snapshot_id": row["data_snapshot_id"].strip(),
                "result_json": _json_value(row["result_json"]),
                "query_plan_json": _json_value(row["query_plan_json"]),
            }
            for row in rows
        ]

    async def _recovery_diagnostic(
        self,
        conn: asyncpg.Connection,
        task_id: str,
        data_snapshot_id: str,
    ) -> str | None:
        """Read the durable replay ledger under the Task recovery lock."""
        step_rows = await conn.fetch(
            """
            SELECT step_key, graph_node_key, status, result_json, result_hash,
                   owning_attempt_id::text AS owning_attempt_id, fencing_epoch
            FROM runtime.step_executions
            WHERE task_id = $1::uuid
            ORDER BY started_at, step_key
            """,
            task_id,
        )
        invocation_rows = await conn.fetch(
            """
            SELECT invocation.id::text AS invocation_id,
                   invocation.status, invocation.result_json,
                   invocation.result_hash,
                   evidence.id::text AS evidence_id,
                   evidence.metric_key, evidence.metric_version, evidence.unit,
                   evidence.current_value, evidence.previous_value,
                   evidence.absolute_change, evidence.change_rate,
                   evidence.row_count, evidence.query_plan_json,
                   evidence.result_json AS evidence_result_json,
                   evidence.result_set_hash AS evidence_result_hash,
                   evidence.data_snapshot_id AS evidence_snapshot_id,
                   evidence.tool_invocation_id::text AS evidence_invocation_id
            FROM harness.tool_invocations AS invocation
            LEFT JOIN harness.evidences AS evidence
              ON evidence.tool_invocation_id = invocation.id
            WHERE invocation.task_id = $1::uuid
            ORDER BY invocation.started_at, invocation.invocation_key
            """,
            task_id,
        )
        evidence_rows = await conn.fetch(
            """
            SELECT id::text AS evidence_id, tool_invocation_id::text AS tool_invocation_id,
                   metric_key, metric_version, unit, current_value, previous_value,
                   absolute_change, change_rate, row_count, query_plan_json,
                   result_json, result_set_hash AS result_hash, data_snapshot_id
            FROM harness.evidences
            WHERE task_id = $1::uuid
            """,
            task_id,
        )
        return validate_recovery_records(
            [
                {**dict(row), "result_json": _json_value(row["result_json"])}
                for row in step_rows
            ],
            [
                {**dict(row), "result_json": _json_value(row["result_json"])}
                for row in invocation_rows
            ],
            [
                {
                    **dict(row),
                    "result_json": _json_value(row["result_json"]),
                    "query_plan_json": _json_value(row["query_plan_json"]),
                }
                for row in evidence_rows
            ],
            data_snapshot_id=data_snapshot_id,
        )

    # -- fenced lifecycle writes ----------------------------------------

    async def confirm_step(
        self,
        lease: AttemptLease,
        *,
        step_key: str,
        graph_node_key: str,
        input_hash: str,
        result: dict[str, Any],
        checkpoint_ref: str | None = None,
    ) -> StepRecord:
        result_json = _canonical(result)
        result_hash = _hash(result)
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await self._require_running_task(conn, lease)
                existing = await conn.fetchrow(
                    """
                    SELECT id::uuid AS id, step_key, graph_node_key, status,
                           result_json, result_hash,
                           owning_attempt_id::text AS owning_attempt_id,
                           fencing_epoch
                    FROM runtime.step_executions
                    WHERE task_id = $1::uuid AND step_key = $2
                    FOR UPDATE
                    """,
                    lease.task_id,
                    step_key,
                )
                if existing is not None and existing["status"] == "confirmed":
                    return StepRecord(
                        step_key=existing["step_key"],
                        graph_node_key=existing["graph_node_key"],
                        status="confirmed",
                        result=_json_value(existing["result_json"]),
                        result_hash=existing["result_hash"],
                        owning_attempt_id=existing["owning_attempt_id"],
                        fencing_epoch=existing["fencing_epoch"],
                    )
                if existing is None:
                    await conn.execute(
                        """
                        INSERT INTO runtime.step_executions
                            (id, task_id, step_key, graph_node_key, input_hash,
                             status, result_ref, result_json, result_hash,
                             checkpoint_ref, owning_attempt_id, fencing_epoch,
                             started_at, completed_at)
                        VALUES ($1::uuid, $2::uuid, $3, $4, $5, 'confirmed', NULL,
                                $6::jsonb, $7, $8, $9::uuid, $10, now(), now())
                        """,
                        str(uuid.uuid4()),
                        lease.task_id,
                        step_key,
                        graph_node_key,
                        input_hash,
                        result_json,
                        result_hash,
                        checkpoint_ref,
                        lease.attempt_id,
                        lease.fencing_epoch,
                    )
                else:
                    await conn.execute(
                        """
                        UPDATE runtime.step_executions
                        SET status = 'confirmed', result_json = $3::jsonb,
                            result_hash = $4, result_ref = NULL,
                            checkpoint_ref = $5, owning_attempt_id = $6::uuid,
                            fencing_epoch = $7, completed_at = now()
                        WHERE id = $1::uuid AND task_id = $2::uuid
                        """,
                        existing["id"],
                        lease.task_id,
                        result_json,
                        result_hash,
                        checkpoint_ref,
                        lease.attempt_id,
                        lease.fencing_epoch,
                    )
                await self.fault_injector.checkpoint(
                    FaultPoint.STEP_CONFIRM_BEFORE_COMMIT,
                    {
                        "task_id": lease.task_id,
                        "attempt_id": lease.attempt_id,
                        "step_key": step_key,
                    },
                )
                await self._append_event(
                    conn,
                    task_id=lease.task_id,
                    attempt_id=lease.attempt_id,
                    event_type="step.completed",
                    payload={
                        "step_key": step_key,
                        "graph_node_key": graph_node_key,
                        "result_hash": result_hash,
                        "status": "confirmed",
                    },
                )
        await self.fault_injector.checkpoint(
            FaultPoint.STEP_CONFIRMED,
            {
                "task_id": lease.task_id,
                "attempt_id": lease.attempt_id,
                "step_key": step_key,
            },
        )
        return StepRecord(
            step_key=step_key,
            graph_node_key=graph_node_key,
            status="confirmed",
            result=result,
            result_hash=result_hash,
            owning_attempt_id=lease.attempt_id,
            fencing_epoch=lease.fencing_epoch,
        )

    async def fail_step(
        self,
        lease: AttemptLease,
        *,
        step_key: str,
        graph_node_key: str,
        input_hash: str,
        error_code: str,
        error_detail: str,
    ) -> None:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await self._require_running_task(conn, lease)
                existing = await conn.fetchrow(
                    "SELECT id::uuid AS id, status FROM runtime.step_executions "
                    "WHERE task_id = $1::uuid AND step_key = $2 FOR UPDATE",
                    lease.task_id,
                    step_key,
                )
                if existing is not None and existing["status"] == "confirmed":
                    return
                if existing is None:
                    await conn.execute(
                        """
                        INSERT INTO runtime.step_executions
                            (id, task_id, step_key, graph_node_key, input_hash,
                             status, owning_attempt_id, fencing_epoch,
                             started_at, completed_at)
                        VALUES ($1::uuid, $2::uuid, $3, $4, $5, 'failed',
                                $6::uuid, $7, now(), now())
                        """,
                        str(uuid.uuid4()),
                        lease.task_id,
                        step_key,
                        graph_node_key,
                        input_hash,
                        lease.attempt_id,
                        lease.fencing_epoch,
                    )
                else:
                    await conn.execute(
                        """
                        UPDATE runtime.step_executions
                        SET status = 'failed', owning_attempt_id = $3::uuid,
                            fencing_epoch = $4, completed_at = now()
                        WHERE id = $1::uuid AND task_id = $2::uuid
                        """,
                        existing["id"],
                        lease.task_id,
                        lease.attempt_id,
                        lease.fencing_epoch,
                    )
                await self._append_event(
                    conn,
                    task_id=lease.task_id,
                    attempt_id=lease.attempt_id,
                    event_type="step.failed",
                    payload={
                        "step_key": step_key,
                        "graph_node_key": graph_node_key,
                        "error_code": error_code,
                        "error_detail": error_detail[:2000],
                    },
                )

    async def complete_task(self, lease: AttemptLease, result: dict[str, Any]) -> bool:
        result_json = _canonical(result)
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await self._require_running_task(conn, lease)
                await self.fault_injector.checkpoint(
                    FaultPoint.TERMINAL_BEFORE_COMMIT,
                    {"task_id": lease.task_id, "attempt_id": lease.attempt_id},
                )
                await conn.execute(
                    """
                    UPDATE runtime.tasks
                    SET status = 'completed', result_json = $2::jsonb,
                        completed_at = now(), error_code = NULL,
                        error_message = NULL, next_retry_at = NULL,
                        updated_at = now(), version = version + 1
                    WHERE id = $1::uuid
                    """,
                    lease.task_id,
                    result_json,
                )
                await conn.execute(
                    """
                    UPDATE runtime.task_attempts
                    SET status = 'succeeded', finished_at = now()
                    WHERE id = $1::uuid
                    """,
                    lease.attempt_id,
                )
                await self._append_event(
                    conn,
                    task_id=lease.task_id,
                    attempt_id=lease.attempt_id,
                    event_type="task.completed",
                    payload={
                        "report_version": result.get("report_version"),
                        "result_hash": _hash(result),
                    },
                )
        return True

    async def fail_task(
        self, lease: AttemptLease, error_code: str, error_detail: str
    ) -> bool:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await self._require_running_task(conn, lease)
                await conn.execute(
                    """
                    UPDATE runtime.tasks
                    SET status = 'failed', error_code = $2, error_message = $3,
                        completed_at = now(), next_retry_at = NULL,
                        updated_at = now(), version = version + 1
                    WHERE id = $1::uuid
                    """,
                    lease.task_id,
                    error_code,
                    error_detail[:2000],
                )
                await conn.execute(
                    """
                    UPDATE runtime.task_attempts
                    SET status = 'failed', finished_at = now(), error_code = $2,
                        error_detail = $3
                    WHERE id = $1::uuid
                    """,
                    lease.attempt_id,
                    error_code,
                    error_detail[:2000],
                )
                await self._append_event(
                    conn,
                    task_id=lease.task_id,
                    attempt_id=lease.attempt_id,
                    event_type="task.failed",
                    payload={
                        "error_code": error_code,
                        "error_detail": error_detail[:2000],
                    },
                )
        return True

    async def retry_task(
        self,
        lease: AttemptLease,
        error_code: str,
        error_detail: str,
        *,
        delay_seconds: float,
    ) -> str:
        """Fail this Attempt and either schedule ``retry_wait`` or fail the Task.

        Returns ``"retry_wait"`` when another Attempt may be scheduled, or
        ``"failed"`` when ``max_attempts`` is exhausted.
        """
        delay = max(0.0, delay_seconds)
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await self._require_running_task(conn, lease)
                task = await conn.fetchrow(
                    "SELECT current_attempt_no, max_attempts "
                    "FROM runtime.tasks WHERE id = $1::uuid",
                    lease.task_id,
                )
                await conn.execute(
                    """
                    UPDATE runtime.task_attempts
                    SET status = 'failed', finished_at = now(), error_code = $2,
                        error_detail = $3
                    WHERE id = $1::uuid
                    """,
                    lease.attempt_id,
                    error_code,
                    error_detail[:2000],
                )
                if task["current_attempt_no"] >= task["max_attempts"]:
                    await conn.execute(
                        """
                        UPDATE runtime.tasks
                        SET status = 'failed', error_code = $2, error_message = $3,
                            completed_at = now(), updated_at = now(),
                            version = version + 1
                        WHERE id = $1::uuid
                        """,
                        lease.task_id,
                        error_code,
                        error_detail[:2000],
                    )
                    await self._append_event(
                        conn,
                        task_id=lease.task_id,
                        attempt_id=lease.attempt_id,
                        event_type="task.failed",
                        payload={
                            "error_code": error_code,
                            "error_detail": error_detail[:2000],
                            "attempts_exhausted": True,
                        },
                    )
                    return "failed"
                await conn.execute(
                    """
                    UPDATE runtime.tasks
                    SET status = 'retry_wait', error_code = $2,
                        error_message = $3,
                        next_retry_at = now()
                            + ($4::double precision * interval '1 second'),
                        updated_at = now(), version = version + 1
                    WHERE id = $1::uuid
                    """,
                    lease.task_id,
                    error_code,
                    error_detail[:2000],
                    delay,
                )
                await self._append_event(
                    conn,
                    task_id=lease.task_id,
                    attempt_id=lease.attempt_id,
                    event_type="task.retry_scheduled",
                    payload={
                        "error_code": error_code,
                        "attempt_no": task["current_attempt_no"],
                        "max_attempts": task["max_attempts"],
                        "delay_seconds": delay,
                    },
                )
                return "retry_wait"

    async def mark_cancelling(self, lease: AttemptLease) -> bool:
        """Apply the framed ``running -> cancelling`` transition."""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                task = await self._lock_live_attempt(conn, lease)
                if task["status"] in TERMINAL_TASK_STATUSES:
                    return False
                if not self._has_cancel_request(task):
                    return False
                if task["status"] == "running":
                    await conn.execute(
                        """
                        UPDATE runtime.tasks
                        SET status = 'cancelling',
                            cancel_requested_at = COALESCE(cancel_requested_at, now()),
                            updated_at = now(), version = version + 1
                        WHERE id = $1::uuid
                        """,
                        lease.task_id,
                    )
                    await self._append_event(
                        conn,
                        task_id=lease.task_id,
                        attempt_id=lease.attempt_id,
                        event_type="task.cancelling",
                        payload={"reason": self._cancel_reason(task)},
                    )
                return True

    async def finalize_cancellation(self, lease: AttemptLease) -> bool:
        """Fenced terminal transition to ``cancelled`` by the owning worker."""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                task = await self._lock_live_attempt(conn, lease)
                if task["status"] in TERMINAL_TASK_STATUSES:
                    return False
                if not self._has_cancel_request(task) and task["status"] != "cancelling":
                    return False
                return await self._finalize_cancellation_locked(conn, lease, task)

    async def _finalize_cancellation_locked(
        self,
        conn: asyncpg.Connection,
        lease: AttemptLease,
        task: asyncpg.Record,
    ) -> bool:
        """Finalize cancellation while the caller holds Task/event locks."""
        reason = self._cancel_reason(task)
        if task["status"] == "running":
            await conn.execute(
                """
                UPDATE runtime.tasks
                SET status = 'cancelling',
                    cancel_requested_at = COALESCE(cancel_requested_at, now()),
                    updated_at = now(), version = version + 1
                WHERE id = $1::uuid
                """,
                lease.task_id,
            )
            await self._append_event(
                conn,
                task_id=lease.task_id,
                attempt_id=lease.attempt_id,
                event_type="task.cancelling",
                payload={"reason": reason},
            )
        await conn.execute(
            """
            UPDATE runtime.tasks
            SET status = 'cancelled',
                cancel_requested_at = COALESCE(cancel_requested_at, now()),
                completed_at = now(), error_code = NULL,
                error_message = NULL, next_retry_at = NULL,
                updated_at = now(), version = version + 1
            WHERE id = $1::uuid
            """,
            lease.task_id,
        )
        await conn.execute(
            """
            UPDATE runtime.task_attempts
            SET status = 'abandoned', finished_at = now(),
                error_code = 'CANCELLED',
                error_detail = 'task cancelled by owner'
            WHERE id = $1::uuid AND status = 'running'
            """,
            lease.attempt_id,
        )
        await self._append_event(
            conn,
            task_id=lease.task_id,
            attempt_id=lease.attempt_id,
            event_type="task.cancelled",
            payload={"reason": reason},
        )
        return True

    async def expire_task(self, lease: AttemptLease) -> bool:
        """Fenced terminal transition to ``failed(DEADLINE_EXCEEDED)``."""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                task = await self._lock_live_attempt(conn, lease)
                if task["status"] in TERMINAL_TASK_STATUSES:
                    return False
                if self._has_cancel_request(task):
                    return await self._finalize_cancellation_locked(conn, lease, task)
                if not task["expired"]:
                    return False
                await conn.execute(
                    """
                    UPDATE runtime.tasks
                    SET status = 'failed', error_code = 'DEADLINE_EXCEEDED',
                        error_message = 'task deadline passed',
                        completed_at = now(), updated_at = now(),
                        version = version + 1
                    WHERE id = $1::uuid
                    """,
                    lease.task_id,
                )
                await conn.execute(
                    """
                    UPDATE runtime.task_attempts
                    SET status = 'failed', finished_at = now(),
                        error_code = 'DEADLINE_EXCEEDED',
                        error_detail = 'task deadline passed'
                    WHERE id = $1::uuid
                    """,
                    lease.attempt_id,
                )
                await self._append_event(
                    conn,
                    task_id=lease.task_id,
                    attempt_id=lease.attempt_id,
                    event_type="task.failed",
                    payload={
                        "error_code": "DEADLINE_EXCEEDED",
                        "error_detail": "task deadline passed",
                    },
                )
                return True

    # -- reconciliation sweep -------------------------------------------

    async def reconcile(self, *, limit: int = 20) -> dict[str, int]:
        """Advance cancellation, deadline, and lost-lease state machine edges.

        Each candidate is handled in its own short transaction. A Task that
        enters ``recovering`` during this pass waits for the next pass to leave
        it, so operators/tests can observe the intermediate state.
        """
        return {
            "deadline_failures": await self._sweep_deadlines(limit),
            "cancelled_unclaimed": await self._sweep_unclaimed_cancellations(limit),
            "recovering_advanced": await self._sweep_recovering(limit),
            "recovered_leases": await self._sweep_expired_leases(limit),
        }

    async def _sweep_deadlines(self, limit: int) -> int:
        handled = 0
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id::text AS id
                FROM runtime.tasks
                WHERE status = ANY($1::text[])
                  AND deadline_at IS NOT NULL AND deadline_at <= now()
                ORDER BY deadline_at, id
                LIMIT $2
                """,
                sorted(ACTIVE_TASK_STATUSES),
                limit,
            )
            for row in rows:
                task_id = row["id"]
                async with conn.transaction():
                    task = await conn.fetchrow(
                        "SELECT status FROM runtime.tasks "
                        "WHERE id = $1::uuid FOR UPDATE SKIP LOCKED",
                        task_id,
                    )
                    if task is None or task["status"] not in ACTIVE_TASK_STATUSES:
                        continue
                    await self._lock_events(conn, task_id)
                    cancel_state = await conn.fetchrow(
                        """
                        SELECT
                            EXISTS (
                                SELECT 1 FROM runtime.task_events
                                WHERE task_id = $1::uuid
                                  AND event_type = 'task.cancelled'
                            ) AS cancelled_event,
                            EXISTS (
                                SELECT 1 FROM runtime.task_events
                                WHERE task_id = $1::uuid
                                  AND event_type = $2
                            ) AS cancel_requested
                        """,
                        task_id,
                        CANCEL_REQUEST_EVENT,
                    )
                    if cancel_state["cancelled_event"]:
                        # Cancellation committed first; materialize it instead
                        # of appending a second terminal event.
                        await conn.execute(
                            """
                            UPDATE runtime.tasks
                            SET status = 'cancelled',
                                cancel_requested_at = COALESCE(
                                    cancel_requested_at, now()),
                                completed_at = now(), error_code = NULL,
                                error_message = NULL, next_retry_at = NULL,
                                updated_at = now(), version = version + 1
                            WHERE id = $1::uuid
                            """,
                            task_id,
                        )
                        await conn.execute(
                            """
                            UPDATE runtime.task_attempts
                            SET status = 'abandoned',
                                finished_at = COALESCE(finished_at, now()),
                                error_code = COALESCE(error_code, 'CANCELLED')
                            WHERE task_id = $1::uuid AND status = 'running'
                            """,
                            task_id,
                        )
                        handled += 1
                        continue
                    if cancel_state["cancel_requested"]:
                        task_state = await conn.fetchrow(
                            """
                            SELECT status, cancel_requested_at,
                                   (deadline_at IS NOT NULL AND deadline_at <= now())
                                       AS expired,
                                   true AS cancel_event,
                                   (SELECT event.payload_json
                                    FROM runtime.task_events AS event
                                    WHERE event.task_id = runtime.tasks.id
                                      AND event.event_type = $2
                                    ORDER BY event.task_event_seq
                                    LIMIT 1) AS cancel_payload
                            FROM runtime.tasks
                            WHERE id = $1::uuid
                            """,
                            task_id,
                            CANCEL_REQUEST_EVENT,
                        )
                        reason = self._cancel_reason(task_state)
                        await conn.execute(
                            """
                            UPDATE runtime.tasks
                            SET status = 'cancelled',
                                cancel_requested_at = COALESCE(cancel_requested_at, now()),
                                completed_at = now(), error_code = NULL,
                                error_message = NULL, next_retry_at = NULL,
                                updated_at = now(), version = version + 1
                            WHERE id = $1::uuid
                            """,
                            task_id,
                        )
                        await conn.execute(
                            """
                            UPDATE runtime.task_attempts
                            SET status = 'abandoned',
                                finished_at = COALESCE(finished_at, now()),
                                error_code = COALESCE(error_code, 'CANCELLED')
                            WHERE task_id = $1::uuid AND status = 'running'
                            """,
                            task_id,
                        )
                        await self._append_event(
                            conn,
                            task_id=task_id,
                            attempt_id=None,
                            event_type='task.cancelled',
                            payload={"reason": reason, "source": "scanner"},
                        )
                        handled += 1
                        continue
                    attempt = await conn.fetchrow(
                        "SELECT id::text AS attempt_id FROM runtime.task_attempts "
                        "WHERE task_id = $1::uuid AND status = 'running' LIMIT 1",
                        task_id,
                    )
                    await conn.execute(
                        """
                        UPDATE runtime.tasks
                        SET status = 'failed', error_code = 'DEADLINE_EXCEEDED',
                            error_message = 'task deadline passed',
                            completed_at = now(), updated_at = now(),
                            version = version + 1
                        WHERE id = $1::uuid
                        """,
                        task_id,
                    )
                    await conn.execute(
                        """
                        UPDATE runtime.task_attempts
                        SET status = 'failed',
                            finished_at = COALESCE(finished_at, now()),
                            error_code = 'DEADLINE_EXCEEDED',
                            error_detail = 'task deadline passed'
                        WHERE task_id = $1::uuid AND status = 'running'
                        """,
                        task_id,
                    )
                    await self._append_event(
                        conn,
                        task_id=task_id,
                        attempt_id=None if attempt is None else attempt["attempt_id"],
                        event_type="task.failed",
                        payload={
                            "error_code": "DEADLINE_EXCEEDED",
                            "error_detail": "task deadline passed",
                            "source": "scanner",
                        },
                    )
                    handled += 1
        return handled

    async def _sweep_unclaimed_cancellations(self, limit: int) -> int:
        handled = 0
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT task.id::text AS id
                FROM runtime.tasks AS task
                WHERE task.status IN ('queued', 'retry_wait', 'recovering')
                  AND EXISTS (
                      SELECT 1 FROM runtime.task_events AS event
                      WHERE event.task_id = task.id
                        AND event.event_type = $1
                  )
                ORDER BY task.created_at, task.id
                LIMIT $2
                """,
                CANCEL_REQUEST_EVENT,
                limit,
            )
            for row in rows:
                task_id = row["id"]
                async with conn.transaction():
                    task = await conn.fetchrow(
                        """
                        SELECT status,
                               EXISTS (
                                   SELECT 1 FROM runtime.task_events AS event
                                   WHERE event.task_id = runtime.tasks.id
                                     AND event.event_type = $2
                               ) AS cancel_event,
                               (SELECT event.payload_json
                                FROM runtime.task_events AS event
                                WHERE event.task_id = runtime.tasks.id
                                  AND event.event_type = $2
                                ORDER BY event.task_event_seq
                                LIMIT 1) AS cancel_payload
                        FROM runtime.tasks
                        WHERE id = $1::uuid
                        FOR UPDATE SKIP LOCKED
                        """,
                        task_id,
                        CANCEL_REQUEST_EVENT,
                    )
                    if (
                        task is None
                        or task["status"] not in {"queued", "retry_wait", "recovering"}
                        or not task["cancel_event"]
                    ):
                        continue
                    await self._lock_events(conn, task_id)
                    reason = self._cancel_reason(task)
                    await conn.execute(
                        """
                        UPDATE runtime.tasks
                        SET status = 'cancelled',
                            cancel_requested_at = COALESCE(cancel_requested_at, now()),
                            completed_at = now(), error_code = NULL,
                            error_message = NULL, next_retry_at = NULL,
                            updated_at = now(), version = version + 1
                        WHERE id = $1::uuid
                        """,
                        task_id,
                    )
                    await conn.execute(
                        """
                        UPDATE runtime.task_attempts
                        SET status = 'abandoned',
                            finished_at = COALESCE(finished_at, now()),
                            error_code = COALESCE(error_code, 'CANCELLED')
                        WHERE task_id = $1::uuid AND status = 'running'
                        """,
                        task_id,
                    )
                    # A non-executing Task cancelled through the API already
                    # has the terminal event; the sweep only materializes the
                    # status column and must not duplicate it.
                    terminal_exists = await conn.fetchval(
                        "SELECT EXISTS (SELECT 1 FROM runtime.task_events "
                        "WHERE task_id = $1::uuid "
                        "AND event_type = 'task.cancelled')",
                        task_id,
                    )
                    if not terminal_exists:
                        await self._append_event(
                            conn,
                            task_id=task_id,
                            attempt_id=None,
                            event_type="task.cancelled",
                            payload={"reason": reason, "source": "scanner"},
                        )
                    handled += 1
        return handled

    async def _sweep_recovering(self, limit: int) -> int:
        handled = 0
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT task.id::text AS id
                FROM runtime.tasks AS task
                WHERE task.status = 'recovering'
                  AND NOT EXISTS (
                      SELECT 1 FROM runtime.task_attempts AS attempt
                      WHERE attempt.task_id = task.id
                        AND attempt.status = 'running'
                  )
                ORDER BY task.updated_at, task.id
                LIMIT $1
                """,
                limit,
            )
            for row in rows:
                task_id = row["id"]
                async with conn.transaction():
                    task = await conn.fetchrow(
                        """
                        SELECT status, current_attempt_no, max_attempts,
                               data_snapshot_id,
                               (deadline_at IS NOT NULL
                                AND deadline_at <= now()) AS expired,
                               cancel_requested_at,
                               EXISTS (
                                   SELECT 1 FROM runtime.task_events AS event
                                   WHERE event.task_id = runtime.tasks.id
                                     AND event.event_type = $2
                               ) AS cancel_event,
                               (SELECT event.payload_json
                                FROM runtime.task_events AS event
                                WHERE event.task_id = runtime.tasks.id
                                  AND event.event_type = $2
                                ORDER BY event.task_event_seq
                                LIMIT 1) AS cancel_payload
                        FROM runtime.tasks
                        WHERE id = $1::uuid
                        FOR UPDATE SKIP LOCKED
                        """,
                        task_id,
                        CANCEL_REQUEST_EVENT,
                    )
                    if task is None or task["status"] != "recovering":
                        continue
                    running = await conn.fetchval(
                        "SELECT EXISTS (SELECT 1 FROM runtime.task_attempts "
                        "WHERE task_id = $1::uuid AND status = 'running')",
                        task_id,
                    )
                    if running:
                        continue
                    await self._lock_events(conn, task_id)
                    diagnostic = None
                    if (
                        not self._has_cancel_request(task)
                        and task["current_attempt_no"] < task["max_attempts"]
                        and not task["expired"]
                    ):
                        diagnostic = await self._recovery_diagnostic(
                            conn, task_id, task["data_snapshot_id"]
                        )
                    if self._has_cancel_request(task):
                        await conn.execute(
                            """
                            UPDATE runtime.tasks
                            SET status = 'cancelled',
                                cancel_requested_at = COALESCE(
                                    cancel_requested_at, now()),
                                completed_at = now(), error_code = NULL,
                                error_message = NULL, next_retry_at = NULL,
                                updated_at = now(), version = version + 1
                            WHERE id = $1::uuid
                            """,
                            task_id,
                        )
                        await self._append_event(
                            conn,
                            task_id=task_id,
                            attempt_id=None,
                            event_type="task.cancelled",
                            payload={
                                "reason": self._cancel_reason(task),
                                "source": "scanner",
                            },
                        )
                    elif diagnostic is not None:
                        detail = f"recovery state is inconsistent: {diagnostic}"
                        await conn.execute(
                            """
                            UPDATE runtime.tasks
                            SET status = 'failed', error_code = 'RECOVERY_STATE_INVALID',
                                error_message = $2, completed_at = now(),
                                updated_at = now(), version = version + 1
                            WHERE id = $1::uuid
                            """,
                            task_id,
                            detail[:2000],
                        )
                        await self._append_event(
                            conn,
                            task_id=task_id,
                            attempt_id=None,
                            event_type="task.failed",
                            payload={
                                "error_code": "RECOVERY_STATE_INVALID",
                                "error_detail": detail[:2000],
                            },
                        )
                    elif (
                        task["current_attempt_no"] < task["max_attempts"]
                        and not task["expired"]
                    ):
                        await self.fault_injector.checkpoint(
                            FaultPoint.RECOVERY_SCAN_BEFORE_COMMIT,
                            {"task_id": task_id, "status": "queued"},
                        )
                        await conn.execute(
                            """
                            UPDATE runtime.tasks
                            SET status = 'queued', next_retry_at = NULL,
                                error_code = NULL, error_message = NULL,
                                updated_at = now(), version = version + 1
                            WHERE id = $1::uuid
                            """,
                            task_id,
                        )
                        await self._append_event(
                            conn,
                            task_id=task_id,
                            attempt_id=None,
                            event_type="task.requeued",
                            payload={
                                "attempt_no": task["current_attempt_no"],
                                "max_attempts": task["max_attempts"],
                            },
                        )
                    else:
                        code = (
                            "DEADLINE_EXCEEDED"
                            if task["expired"]
                            else "ATTEMPTS_EXHAUSTED"
                        )
                        detail = (
                            "task deadline passed during recovery"
                            if task["expired"]
                            else "no attempts remain for recovery"
                        )
                        await conn.execute(
                            """
                            UPDATE runtime.tasks
                            SET status = 'failed', error_code = $2,
                                error_message = $3, completed_at = now(),
                                updated_at = now(), version = version + 1
                            WHERE id = $1::uuid
                            """,
                            task_id,
                            code,
                            detail,
                        )
                        await self._append_event(
                            conn,
                            task_id=task_id,
                            attempt_id=None,
                            event_type="task.failed",
                            payload={"error_code": code, "error_detail": detail},
                        )
                    handled += 1
        return handled

    async def _sweep_expired_leases(self, limit: int) -> int:
        handled = 0
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT attempt.task_id::text AS id
                FROM runtime.task_attempts AS attempt
                JOIN runtime.tasks AS task ON task.id = attempt.task_id
                WHERE attempt.status = 'running'
                  AND attempt.lease_expires_at <= now()
                  AND task.status IN ('running', 'cancelling')
                GROUP BY attempt.task_id
                ORDER BY attempt.task_id
                LIMIT $1
                """,
                limit,
            )
            for row in rows:
                task_id = row["id"]
                async with conn.transaction():
                    task = await conn.fetchrow(
                        "SELECT status FROM runtime.tasks "
                        "WHERE id = $1::uuid FOR UPDATE SKIP LOCKED",
                        task_id,
                    )
                    if task is None:
                        continue
                    await self._lock_events(conn, task_id)
                    expired_attempts = await conn.fetch(
                        """
                        SELECT id::text AS id, attempt_no
                        FROM runtime.task_attempts
                        WHERE task_id = $1::uuid AND status = 'running'
                          AND lease_expires_at <= now()
                        ORDER BY attempt_no
                        """,
                        task_id,
                    )
                    if not expired_attempts:
                        continue
                    await conn.execute(
                        """
                        UPDATE runtime.task_attempts
                        SET status = 'abandoned', finished_at = now(),
                            error_code = COALESCE(error_code, 'LEASE_LOST')
                        WHERE task_id = $1::uuid AND status = 'running'
                          AND lease_expires_at <= now()
                        """,
                        task_id,
                    )
                    if task["status"] in {"running", "cancelling"}:
                        await conn.execute(
                            """
                            UPDATE runtime.tasks
                            SET status = 'recovering', updated_at = now(),
                                version = version + 1
                            WHERE id = $1::uuid
                            """,
                            task_id,
                        )
                        await self._append_event(
                            conn,
                            task_id=task_id,
                            attempt_id=None,
                            event_type="task.recovering",
                            payload={
                                "previous_status": task["status"],
                                "attempt_nos": [
                                    attempt["attempt_no"]
                                    for attempt in expired_attempts
                                ],
                                "error_code": "LEASE_LOST",
                            },
                        )
                    handled += 1
        return handled

    async def abandon_attempt(
        self, lease: AttemptLease, *, error_code: str = "LEASE_LOST"
    ) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE runtime.task_attempts
                SET status = CASE WHEN status = 'running' THEN 'abandoned' ELSE status END,
                    finished_at = COALESCE(finished_at, now()),
                    error_code = COALESCE(error_code, $5)
                WHERE id = $1::uuid AND task_id = $2::uuid
                  AND lease_token = $3::uuid AND fencing_epoch = $4
                """,
                lease.attempt_id,
                lease.task_id,
                lease.lease_token,
                lease.fencing_epoch,
                error_code,
            )

    # -- internals -------------------------------------------------------

    async def _require_running_task(
        self, conn: asyncpg.Connection, lease: AttemptLease
    ) -> None:
        task = await self._lock_task_state(conn, lease.task_id)
        if task is None:
            raise LeaseLostError("task no longer exists")
        if (
            task["status"] == "cancelling"
            or task["cancel_requested_at"] is not None
            or task["cancel_event"]
        ):
            raise TaskNotRunningError("CANCELLED", "task cancellation was requested")
        if task["expired"]:
            raise TaskNotRunningError("DEADLINE_EXCEEDED", "task deadline has passed")
        if task["status"] != "running":
            raise TaskNotRunningError(
                "TASK_NOT_RUNNING", f"task status is {task['status']}"
            )
        attempt = await conn.fetchrow(
            """
            SELECT 1 FROM runtime.task_attempts
            WHERE id = $1::uuid AND task_id = $2::uuid
              AND lease_token = $3::uuid AND fencing_epoch = $4
              AND status = 'running' AND lease_expires_at > now()
            FOR UPDATE
            """,
            lease.attempt_id,
            lease.task_id,
            lease.lease_token,
            lease.fencing_epoch,
        )
        if attempt is None:
            raise LeaseLostError("attempt lease, identity, or fencing epoch is stale")

    async def _lock_live_attempt(
        self, conn: asyncpg.Connection, lease: AttemptLease
    ) -> asyncpg.Record:
        """Lock the Task, take the per-Task event lock, and verify the lease.

        The Task row is locked before the advisory lock. Every writer uses that
        order; the API cancellation path takes only the advisory lock, so it
        can never form a lock cycle with a worker.
        """
        task = await self._lock_task_state(conn, lease.task_id)
        if task is None:
            raise LeaseLostError("task no longer exists")
        attempt = await conn.fetchrow(
            """
            SELECT 1 FROM runtime.task_attempts
            WHERE id = $1::uuid AND task_id = $2::uuid
              AND lease_token = $3::uuid AND fencing_epoch = $4
              AND status = 'running' AND lease_expires_at > now()
            FOR UPDATE
            """,
            lease.attempt_id,
            lease.task_id,
            lease.lease_token,
            lease.fencing_epoch,
        )
        if attempt is None:
            raise LeaseLostError("attempt lease, identity, or fencing epoch is stale")
        return task

    async def _lock_task_state(
        self, conn: asyncpg.Connection, task_id: str
    ) -> asyncpg.Record | None:
        """Row-lock the Task, then serialize event writers, then re-read."""
        await conn.fetchrow(
            "SELECT id FROM runtime.tasks WHERE id = $1::uuid FOR UPDATE",
            task_id,
        )
        await self._lock_events(conn, task_id)
        return await conn.fetchrow(
            """
            SELECT status, cancel_requested_at,
                   (deadline_at IS NOT NULL AND deadline_at <= now()) AS expired,
                   EXISTS (
                       SELECT 1 FROM runtime.task_events AS event
                       WHERE event.task_id = runtime.tasks.id
                         AND event.event_type = $2
                   ) AS cancel_event,
                   (SELECT event.payload_json
                    FROM runtime.task_events AS event
                    WHERE event.task_id = runtime.tasks.id
                      AND event.event_type = $2
                    ORDER BY event.task_event_seq
                    LIMIT 1) AS cancel_payload
            FROM runtime.tasks
            WHERE id = $1::uuid
            """,
            task_id,
            CANCEL_REQUEST_EVENT,
        )

    @staticmethod
    async def _lock_events(conn: asyncpg.Connection, task_id: str) -> None:
        await conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended($1::text, 0))", task_id
        )

    @staticmethod
    def _has_cancel_request(task: asyncpg.Record | dict[str, Any]) -> bool:
        return bool(task["cancel_requested_at"] is not None or task["cancel_event"])

    @staticmethod
    def _cancel_reason(task: asyncpg.Record | dict[str, Any]) -> str | None:
        payload = task.get("cancel_payload") if isinstance(task, dict) else task["cancel_payload"]
        if payload is None:
            return None
        try:
            value = _json_value(payload)
        except (TypeError, ValueError):
            return None
        if isinstance(value, dict):
            reason = value.get("reason")
            return reason if isinstance(reason, str) else None
        return None

    async def _append_event(
        self,
        conn: asyncpg.Connection,
        *,
        task_id: str,
        attempt_id: str | None,
        event_type: str,
        payload: dict[str, Any],
    ) -> int:
        await self._lock_events(conn, task_id)
        seq = await conn.fetchval(
            "SELECT COALESCE(MAX(task_event_seq), 0) + 1 FROM runtime.task_events "
            "WHERE task_id = $1::uuid",
            task_id,
        )
        await conn.execute(
            """
            INSERT INTO runtime.task_events
                (task_id, attempt_id, task_event_seq, event_type, payload_json)
            VALUES ($1::uuid, $2::uuid, $3, $4, $5::jsonb)
            """,
            task_id,
            attempt_id,
            seq,
            event_type,
            _canonical(payload),
        )
        return seq
