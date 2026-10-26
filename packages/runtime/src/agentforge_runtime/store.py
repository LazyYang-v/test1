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

from agentforge_core.evaluation import evaluate_case

from agentforge_runtime.contracts import AttemptLease, StepRecord
from agentforge_runtime.errors import LeaseLostError, TaskNotRunningError


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
    ) -> None:
        self.pool = pool
        self.worker_id = worker_id
        self.lease_seconds = lease_seconds

    async def close(self) -> None:
        await self.pool.close()

    # -- claim and lease -------------------------------------------------

    async def claim(self) -> AttemptLease | None:
        """Claim one queued/retry-due Task with FOR UPDATE SKIP LOCKED.

        The Task lock, Attempt insert, and started event all commit together;
        a competing worker either skips the locked row or sees it as running.
        """
        attempt_id = str(uuid.uuid4())
        lease_token = str(uuid.uuid4())
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                task = await conn.fetchrow(
                    """
                    WITH candidate AS (
                        SELECT id
                        FROM runtime.tasks
                        WHERE (
                                status = 'queued'
                                OR (status = 'retry_wait' AND next_retry_at <= now())
                              )
                          AND cancel_requested_at IS NULL
                          AND (deadline_at IS NULL OR deadline_at > now())
                        ORDER BY priority DESC, created_at, id
                        FOR UPDATE SKIP LOCKED
                        LIMIT 1
                    )
                    UPDATE runtime.tasks AS task
                    SET status = 'running',
                        current_attempt_no = task.current_attempt_no + 1,
                        version = task.version + 1,
                        updated_at = now()
                    FROM candidate
                    WHERE task.id = candidate.id
                    RETURNING task.id, task.current_attempt_no
                    """
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
                return _row_to_lease(row)

    async def heartbeat(self, lease: AttemptLease) -> bool:
        """Renew the lease by a fixed interval using database time."""
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
                        AND task.status = 'running'
                        AND task.cancel_requested_at IS NULL
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
                    )
                    """,
                    lease.attempt_id,
                    lease.task_id,
                    lease.lease_token,
                    lease.fencing_epoch,
                )
            )

    # -- reads -----------------------------------------------------------

    async def load_task(self, task_id: str) -> dict[str, Any]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id::text AS task_id, owner_user_id::text AS owner_user_id,
                       agent_version_id::text AS agent_version_id, task_type,
                       status, input_json, input_hash, region_scope_json,
                       authorization_snapshot, data_snapshot_id, priority,
                       current_attempt_no, max_attempts, deadline_at, created_at,
                       eval_run_id AS evaluation_run_id
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
        if result.get("evaluation_run_id") is not None:
            result["evaluation_run_id"] = str(result["evaluation_run_id"])
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
                await conn.execute(
                    """
                    UPDATE runtime.tasks
                    SET status = 'completed', result_json = $2::jsonb,
                        completed_at = now(), error_code = NULL,
                        error_message = NULL, updated_at = now(),
                        version = version + 1
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
                await self._complete_evaluation_case(conn, lease.task_id, result)
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
                        completed_at = now(), updated_at = now(),
                        version = version + 1
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
                await self._complete_evaluation_case(
                    conn, lease.task_id, None, task_error_code=error_code
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

    async def _complete_evaluation_case(
        self,
        conn: asyncpg.Connection,
        task_id: str,
        task_result: dict[str, Any] | None,
        *,
        task_error_code: str | None = None,
    ) -> None:
        task = await conn.fetchrow(
            """
            SELECT task.eval_run_id::text AS evaluation_run_id,
                   task.status, task.result_json, task.error_code,
                   task.data_snapshot_id
            FROM runtime.tasks AS task WHERE task.id = $1::uuid
            """,
            task_id,
        )
        if task is None or task["evaluation_run_id"] is None:
            return

        case = await conn.fetchrow(
            """
            SELECT result.id::text AS result_id, result.eval_run_id::text AS run_id,
                   result.created_at AS started_at, case_row.case_key, case_row.input_json,
                   case_row.expected_json, case_row.assertions_json,
                   case_row.data_snapshot_ref
            FROM evaluation.case_results AS result
            JOIN evaluation.cases AS case_row ON case_row.id = result.case_id
            WHERE result.task_id = $1::uuid AND result.status = 'pending'
            FOR UPDATE OF result
            """,
            task_id,
        )
        if case is None:
            return
        evidence_rows = await conn.fetch(
            """
            SELECT id::text AS evidence_id, task_id::text AS task_id,
                   data_snapshot_id, query_plan_json
            FROM harness.evidences WHERE task_id = $1::uuid
            ORDER BY created_at, id
            """,
            task_id,
        )
        case_data = {
            "case_key": case["case_key"],
            "expected": _json_value(case["expected_json"]),
            "assertions": _json_value(case["assertions_json"]),
            "data_snapshot_ref": case["data_snapshot_ref"].strip(),
            "input": _json_value(case["input_json"]),
        }
        result = evaluate_case(
            case_data,
            task_id=task_id,
            task_status=task["status"],
            task_result=task_result if task_error_code is None else None,
            task_error_code=task_error_code,
            task_snapshot_id=task["data_snapshot_id"].strip(),
            evidences=[
                {
                    **dict(evidence),
                    "data_snapshot_id": evidence["data_snapshot_id"].strip(),
                    "query_plan_json": _json_value(evidence["query_plan_json"]),
                }
                for evidence in evidence_rows
            ],
        )
        await conn.execute(
            """
            UPDATE evaluation.case_results
            SET status = $2, score_json = $3::jsonb, failure_codes = $4::text[],
                duration_ms = GREATEST(0, floor(extract(epoch FROM (now() - $5)) * 1000)::integer)
            WHERE id = $1::uuid AND status = 'pending'
            """,
            case["result_id"],
            result["status"],
            _canonical(result["score_json"]),
            result["failure_codes"],
            case["started_at"],
        )
        pending = await conn.fetchval(
            "SELECT count(*) FROM evaluation.case_results "
            "WHERE eval_run_id = $1::uuid AND status = 'pending'",
            case["run_id"],
        )
        if pending == 0:
            summary = await conn.fetchrow(
                """
                SELECT count(*)::integer AS total,
                       count(*) FILTER (WHERE status = 'passed')::integer AS passed,
                       count(*) FILTER (WHERE status = 'failed')::integer AS failed
                FROM evaluation.case_results WHERE eval_run_id = $1::uuid
                """,
                case["run_id"],
            )
            passed = summary["passed"]
            total = summary["total"]
            await conn.execute(
                """
                UPDATE evaluation.runs
                SET status = CASE WHEN $1 = $2 THEN 'completed' ELSE 'failed' END,
                    finished_at = now(),
                    summary_json = jsonb_build_object(
                        'total', $1, 'passed', $2, 'failed', $3, 'pending', 0,
                        'score', to_char($2::numeric / NULLIF($1, 0), '0.0000'))
                WHERE id = $4::uuid AND status <> 'completed'
                """,
                total, passed, summary["failed"], case["run_id"],
            )

    async def _require_running_task(
        self, conn: asyncpg.Connection, lease: AttemptLease
    ) -> None:
        task = await conn.fetchrow(
            """
            SELECT status, cancel_requested_at, deadline_at,
                   (deadline_at IS NOT NULL AND deadline_at <= now()) AS expired
            FROM runtime.tasks WHERE id = $1::uuid FOR UPDATE
            """,
            lease.task_id,
        )
        if task is None:
            raise LeaseLostError("task no longer exists")
        if task["status"] != "running":
            raise TaskNotRunningError(
                "TASK_NOT_RUNNING", f"task status is {task['status']}"
            )
        if task["cancel_requested_at"] is not None:
            raise TaskNotRunningError("CANCELLED", "task cancellation was requested")
        if task["expired"]:
            raise TaskNotRunningError("DEADLINE_EXCEEDED", "task deadline has passed")
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

    async def _append_event(
        self,
        conn: asyncpg.Connection,
        *,
        task_id: str,
        attempt_id: str | None,
        event_type: str,
        payload: dict[str, Any],
    ) -> int:
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
