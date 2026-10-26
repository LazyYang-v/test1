"""Owner-scoped task creation and query routes."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import uuid
from datetime import UTC, date, datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import asyncpg
import jwt
from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agentforge_api.auth import ApiError, current_principal

router = APIRouter(prefix="/api/v1/tasks", tags=["tasks"])
_SSE_BEARER = HTTPBearer(auto_error=False)
_EVENT_CHANNEL = "agentforge_task_events"
_EVENT_POLL_SECONDS = 10.0
_MAX_EVENT_ID = 9_223_372_036_854_775_807
_EVENT_DB_TIMEOUT_SECONDS = 1.0
_EVENT_REAUTH_SECONDS = 30.0
_EVENT_RETRY_SECONDS = 1.0
_TRANSIENT_EVENT_DB_ERRORS = (
    OSError,
    asyncpg.InterfaceError,
    asyncpg.PostgresConnectionError,
    asyncpg.CannotConnectNowError,
    asyncpg.TooManyConnectionsError,
    asyncpg.AdminShutdownError,
    asyncpg.CrashShutdownError,
    asyncpg.QueryCanceledError,
    asyncio.TimeoutError,
)

TERMINAL_TASK_STATUSES = frozenset({"completed", "failed", "cancelled"})
CANCEL_REQUEST_EVENT = "task.cancel.requested"
CANCELLABLE_TASK_STATUSES = frozenset(
    {"queued", "retry_wait", "running", "recovering"}
)


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _json_value(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


async def _notify_task_event(conn: asyncpg.Connection, task_id: str, sequence: int) -> None:
    await conn.execute(
        "SELECT pg_notify($1, $2)",
        _EVENT_CHANNEL,
        _canonical({"task_id": task_id, "seq": sequence}),
    )


class DateRange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: date
    end_exclusive: date
    timezone: str = Field(min_length=1, max_length=128)

    @field_validator("start", "end_exclusive", mode="before")
    @classmethod
    def require_iso_date(cls, value: Any) -> date:
        if not isinstance(value, str):
            raise ValueError("date range values must be ISO dates")
        parsed = date.fromisoformat(value)
        if parsed.isoformat() != value:
            raise ValueError("date range values must use YYYY-MM-DD")
        return parsed

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("timezone must name an IANA timezone") from None
        return value

    @model_validator(mode="after")
    def validate_range(self):
        duration = (self.end_exclusive - self.start).days
        if duration <= 0:
            raise ValueError("end_exclusive must be after start")
        if duration > 90:
            raise ValueError("date range cannot exceed 90 calendar days")
        return self

    def canonical_json(self) -> dict[str, str]:
        return {
            "start": self.start.isoformat(),
            "end_exclusive": self.end_exclusive.isoformat(),
            "timezone": self.timezone,
        }


class TaskInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=10000)
    region_codes: list[str] = Field(min_length=1, max_length=100)
    date_range: DateRange
    compare_mode: Literal["previous_period"] = "previous_period"

    @field_validator("question")
    @classmethod
    def normalize_question(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("question must not be blank")
        return value

    @field_validator("region_codes")
    @classmethod
    def normalize_regions(cls, value: list[str]) -> list[str]:
        normalized = [region.strip() for region in value]
        if any(not region for region in normalized):
            raise ValueError("region codes must not be blank")
        if len(set(normalized)) != len(normalized):
            raise ValueError("region codes must not contain duplicates")
        return sorted(normalized)

    def canonical_json(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "region_codes": self.region_codes,
            "date_range": self.date_range.canonical_json(),
            "compare_mode": self.compare_mode,
        }


class ExecutionOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_attempts: int = Field(default=3, ge=1)
    timeout_seconds: int = Field(default=300, ge=1)

    @field_validator("max_attempts")
    @classmethod
    def cap_attempts(cls, value: int) -> int:
        return min(value, 3)

    @field_validator("timeout_seconds")
    @classmethod
    def cap_timeout(cls, value: int) -> int:
        return min(value, 300)


class CreateTaskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_key: str = Field(min_length=1, max_length=128)
    task_type: Literal["business_analysis"]
    input: TaskInput
    execution: ExecutionOptions = Field(default_factory=ExecutionOptions)

    @field_validator("agent_key", "task_type")
    @classmethod
    def trim_nonempty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("value must not be blank")
        return value

    def canonical_json(self) -> dict[str, Any]:
        return {
            "agent_key": self.agent_key,
            "task_type": self.task_type,
            "input": self.input.canonical_json(),
            "execution": self.execution.model_dump(mode="json"),
        }


class CancelTaskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=2000)
    expected_version: int = Field(ge=1)

    @field_validator("reason")
    @classmethod
    def normalize_reason(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("reason must not be blank")
        return value


def _task_response(
    task_id: str, *, agent_version: str, created_at: datetime
) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "status": "queued",
        "agent_version": agent_version,
        "created_at": created_at.isoformat(),
        "links": {
            "self": f"/api/v1/tasks/{task_id}",
            "events": f"/api/v1/tasks/{task_id}/events",
        },
    }


@router.post("", status_code=202)
async def create_task(
    body: CreateTaskRequest,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    principal: dict[str, Any] = Depends(current_principal),
):
    if idempotency_key is None or not idempotency_key.strip():
        raise ApiError(400, "INVALID_REQUEST", "Idempotency-Key header is required")
    idempotency_key = idempotency_key.strip()
    if len(idempotency_key) > 255:
        raise ApiError(400, "INVALID_REQUEST", "Idempotency-Key is too long")

    canonical = body.canonical_json()
    request_hash = _hash(canonical)
    task_id = str(uuid.uuid4())
    idempotency_id = str(uuid.uuid4())
    async with request.app.state.connect() as conn:
        async with conn.transaction():
            # Reserve inside this transaction so a concurrent duplicate waits on
            # the unique key and replays after the first request commits.
            claimed = await conn.fetchval(
                """
                INSERT INTO runtime.idempotency_records
                    (id, actor_user_id, operation, idempotency_key, request_hash,
                     status, expires_at)
                VALUES ($1::uuid, $2::uuid, 'task.create', $3, $4,
                        'processing', now() + interval '24 hours')
                ON CONFLICT (actor_user_id, operation, idempotency_key) DO UPDATE
                SET id = EXCLUDED.id, request_hash = EXCLUDED.request_hash,
                    status = 'processing', resource_id = NULL,
                    response_status = NULL, response_json = NULL,
                    expires_at = EXCLUDED.expires_at, updated_at = now()
                WHERE runtime.idempotency_records.expires_at <= now()
                RETURNING id::text
                """,
                idempotency_id,
                principal["id"],
                idempotency_key,
                request_hash,
            )
            if claimed is None:
                existing = await conn.fetchrow(
                    """
                    SELECT request_hash, status, response_status, response_json
                    FROM runtime.idempotency_records
                    WHERE actor_user_id = $1::uuid AND operation = 'task.create'
                      AND idempotency_key = $2
                    FOR UPDATE
                    """,
                    principal["id"],
                    idempotency_key,
                )
                if existing is None:
                    raise ApiError(503, "IDEMPOTENCY_RETRY", "Retry task creation", retryable=True)
                if existing["request_hash"].strip() != request_hash:
                    raise ApiError(
                        409,
                        "IDEMPOTENCY_CONFLICT",
                        "Idempotency-Key was used for a different request",
                    )
                if existing["status"] != "completed" or existing["response_json"] is None:
                    raise ApiError(503, "IDEMPOTENCY_RETRY", "Retry task creation", retryable=True)
                return JSONResponse(
                    status_code=existing["response_status"],
                    content=json.loads(_canonical(_json_value(existing["response_json"]))),
                )

            agent = await conn.fetchrow(
                """
                SELECT id::text AS id, version FROM registry.agent_versions
                WHERE agent_key = $1 AND status = 'published'
                ORDER BY published_at DESC, created_at DESC, version DESC LIMIT 1
                """,
                body.agent_key,
            )
            if agent is None:
                raise ApiError(400, "INVALID_REQUEST", "agent_key does not resolve to a published Agent")

            grants = await conn.fetch(
                """
                SELECT region_code, permission
                FROM identity.region_grants
                WHERE user_id = $1::uuid AND revoked_at IS NULL
                  AND permission IN ('read', 'admin')
                  AND region_code = ANY($2::text[])
                ORDER BY region_code, permission
                """,
                principal["id"], body.input.region_codes,
            )
            grant_regions = {grant["region_code"] for grant in grants}
            denied_region = next(
                (region for region in body.input.region_codes if region not in grant_regions),
                None,
            )
            if denied_region is not None:
                # The audit record commits independently; the outer transaction
                # then rolls back the reservation and all task writes.
                async with request.app.state.connect() as audit_conn:
                    await audit_conn.execute(
                        """
                        INSERT INTO audit.audit_events
                            (actor_type, actor_id, action, resource_type, resource_id,
                             decision, reason_code, metadata_json, trace_id)
                        VALUES ('user', $1::uuid, 'region.authorization', 'region', $2,
                                'denied', 'REGION_ACCESS_DENIED',
                                jsonb_build_object('region_code', $2::text), $3)
                        """,
                        principal["id"], denied_region, request.state.request_id,
                    )
                raise ApiError(
                    403, "REGION_ACCESS_DENIED", "Access to this region is denied"
                )

            snapshot = await conn.fetchrow(
                """
                SELECT snapshot.data_snapshot_id
                FROM business.data_snapshots AS snapshot
                JOIN business.dataset_loads AS load
                  ON load.data_snapshot_id = snapshot.data_snapshot_id
                WHERE load.status = 'complete'
                ORDER BY snapshot.created_at DESC, snapshot.data_snapshot_id
                LIMIT 1
                """
            )
            if snapshot is None:
                raise ApiError(
                    503,
                    "DATA_SNAPSHOT_UNAVAILABLE",
                    "No complete data snapshot is available",
                    retryable=True,
                )

            authorization_snapshot = [
                {"region_code": grant["region_code"], "permission": grant["permission"]}
                for grant in grants
            ]
            normalized_input = body.input.canonical_json()
            try:
                created_at = await conn.fetchval(
                    """
                    INSERT INTO runtime.tasks
                        (id, owner_user_id, agent_version_id, task_type, status,
                         input_json, input_hash, region_scope_json, authorization_snapshot,
                         data_snapshot_id, max_attempts, deadline_at)
                    VALUES ($1::uuid, $2::uuid, $3::uuid, $4, 'queued', $5::jsonb, $6,
                            $7::jsonb, $8::jsonb, $9, $10,
                            now() + ($11 * interval '1 second'))
                    RETURNING created_at
                    """,
                    task_id,
                    principal["id"],
                    agent["id"],
                    body.task_type,
                    _canonical(normalized_input),
                    _hash(normalized_input),
                    _canonical(body.input.region_codes),
                    _canonical(authorization_snapshot),
                    snapshot["data_snapshot_id"],
                    body.execution.max_attempts,
                    body.execution.timeout_seconds,
                )
            except asyncpg.CheckViolationError as error:
                if error.constraint_name != "tasks_agent_must_be_published":
                    raise
                raise ApiError(
                    400, "INVALID_REQUEST", "agent_key does not resolve to a published Agent"
                ) from None
            await conn.execute(
                """
                INSERT INTO runtime.task_events (task_id, task_event_seq, event_type,
                                                 payload_json, trace_id)
                VALUES ($1::uuid, 1, 'task.created', $2::jsonb, $3)
                """,
                task_id,
                _canonical({"status": "queued"}),
                request.state.request_id,
            )
            await _notify_task_event(conn, task_id, 1)
            await conn.execute(
                """
                INSERT INTO audit.audit_events
                    (actor_type, actor_id, action, resource_type, resource_id,
                     decision, metadata_json, trace_id)
                VALUES ('user', $1::uuid, 'task.created', 'task', $2,
                        'allowed', $3::jsonb, $4)
                """,
                principal["id"],
                task_id,
                _canonical({
                    "agent_version_id": agent["id"],
                    "task_type": body.task_type,
                    "region_codes": body.input.region_codes,
                    "data_snapshot_id": snapshot["data_snapshot_id"].strip(),
                }),
                request.state.request_id,
            )
            response = _task_response(
                task_id, agent_version=agent["version"], created_at=created_at
            )
            await conn.execute(
                """
                UPDATE runtime.idempotency_records
                SET status = 'completed', resource_id = $2::uuid,
                    response_status = 202, response_json = $3::jsonb, updated_at = now()
                WHERE id = $1::uuid
                """,
                claimed,
                task_id,
                _canonical(response),
            )
    return JSONResponse(status_code=202, content=json.loads(_canonical(response)))


@router.post("/{task_id}/cancel", status_code=202)
async def cancel_task(
    task_id: uuid.UUID,
    body: CancelTaskRequest,
    request: Request,
    principal: dict[str, Any] = Depends(current_principal),
):
    """Record an owner-scoped, optimistic-version cancellation request.

    The API role is intentionally append-only for Runtime state: it persists
    the request event inside a per-Task advisory lock and the Worker state
    machine materializes ``cancelling``/``cancelled``. This keeps terminal
    transitions fenced by the owning attempt and preserves the least-privilege
    role boundary (the API never gains UPDATE on ``runtime.tasks``).
    """
    task_id_str = str(task_id)
    async with request.app.state.connect() as conn:
        async with conn.transaction():
            await conn.fetchval(
                "SELECT pg_advisory_xact_lock(hashtextextended($1::text, 0))",
                task_id_str,
            )
            row = await conn.fetchrow(
                """
                SELECT task.status, task.version,
                       (task.deadline_at IS NOT NULL
                        AND task.deadline_at <= now()) AS expired,
                       EXISTS (
                           SELECT 1 FROM runtime.task_events AS event
                           WHERE event.task_id = task.id
                             AND event.event_type = $3
                       ) AS cancel_requested
                FROM runtime.tasks AS task
                WHERE task.id = $1::uuid AND task.owner_user_id = $2::uuid
                """,
                task_id_str,
                principal["id"],
                CANCEL_REQUEST_EVENT,
            )
            if row is None:
                raise ApiError(404, "TASK_NOT_FOUND", "Task not found")
            if row["status"] in TERMINAL_TASK_STATUSES:
                raise ApiError(
                    409,
                    "INVALID_STATE_TRANSITION",
                    "Task is already in a terminal state",
                    details={"status": row["status"]},
                )
            if row["version"] != body.expected_version:
                raise ApiError(
                    409,
                    "INVALID_STATE_TRANSITION",
                    "expected_version does not match the current task version",
                    details={"current_version": row["version"]},
                )
            if row["status"] not in CANCELLABLE_TASK_STATUSES:
                raise ApiError(
                    409,
                    "INVALID_STATE_TRANSITION",
                    "Task cannot be cancelled from its current state",
                    details={"status": row["status"]},
                )
            if row["expired"]:
                raise ApiError(
                    409,
                    "INVALID_STATE_TRANSITION",
                    "Task deadline has already passed",
                )
            if row["cancel_requested"]:
                raise ApiError(
                    409,
                    "INVALID_STATE_TRANSITION",
                    "Task cancellation is already in progress",
                )

            # A Task that is not executing has no attempt that must stop, so
            # cancellation is decided immediately. A running Task enters
            # ``cancelling`` and the owning worker performs the fenced terminal
            # transition. The API role is append-only for Runtime state; the
            # Worker scanner materializes the status column shortly after.
            target_status = (
                "cancelling" if row["status"] == "running" else "cancelled"
            )
            seq = await conn.fetchval(
                "SELECT COALESCE(MAX(task_event_seq), 0) + 1 "
                "FROM runtime.task_events WHERE task_id = $1::uuid",
                task_id_str,
            )
            await conn.execute(
                """
                INSERT INTO runtime.task_events
                    (task_id, attempt_id, task_event_seq, event_type,
                     payload_json, trace_id)
                VALUES ($1::uuid, NULL, $2, $3, $4::jsonb, $5)
                """,
                task_id_str,
                seq,
                CANCEL_REQUEST_EVENT,
                _canonical({
                    "reason": body.reason,
                    "expected_version": body.expected_version,
                    "requested_by": principal["id"],
                    "target_status": target_status,
                }),
                request.state.request_id,
            )
            await _notify_task_event(conn, task_id_str, seq)
            if target_status == "cancelled":
                await conn.execute(
                    """
                    INSERT INTO runtime.task_events
                        (task_id, attempt_id, task_event_seq, event_type,
                         payload_json, trace_id)
                    VALUES ($1::uuid, NULL, $2, 'task.cancelled', $3::jsonb, $4)
                    """,
                    task_id_str,
                    seq + 1,
                    _canonical({"reason": body.reason, "source": "api"}),
                    request.state.request_id,
                )
                await _notify_task_event(conn, task_id_str, seq + 1)
            await conn.execute(
                """
                INSERT INTO audit.audit_events
                    (actor_type, actor_id, action, resource_type, resource_id,
                     decision, metadata_json, trace_id)
                VALUES ('user', $1::uuid, 'task.cancel', 'task', $2,
                        'allowed', $3::jsonb, $4)
                """,
                principal["id"],
                task_id_str,
                _canonical({
                    "reason": body.reason,
                    "expected_version": body.expected_version,
                    "target_status": target_status,
                }),
                request.state.request_id,
            )
    return JSONResponse(
        status_code=202,
        content={
            "task_id": task_id_str,
            "status": target_status,
            "cancel_requested": True,
            "version": row["version"],
            "links": {
                "self": f"/api/v1/tasks/{task_id_str}",
                "events": f"/api/v1/tasks/{task_id_str}/events",
            },
        },
    )


@router.get("")
async def list_tasks(
    request: Request,
    status: str | None = Query(default=None),
    created_after: datetime | None = Query(default=None),
    created_before: datetime | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=100_000),
    principal: dict[str, Any] = Depends(current_principal),
):
    if status is not None and status not in {
        "queued", "running", "retry_wait", "recovering", "cancelling",
        "completed", "failed", "cancelled",
    }:
        raise ApiError(400, "INVALID_REQUEST", "Unknown task status")
    for value in (created_after, created_before):
        if value is not None and value.tzinfo is None:
            raise ApiError(400, "INVALID_REQUEST", "Date filters must include a timezone")
    if created_after and created_before and created_after >= created_before:
        raise ApiError(400, "INVALID_REQUEST", "created_after must be before created_before")

    # Project a durable cancellation request onto the externally visible
    # status until the Worker state machine materializes the column.
    effective_status = f"""
        CASE
            WHEN EXISTS (
                SELECT 1 FROM runtime.task_events AS event
                WHERE event.task_id = runtime.tasks.id
                  AND event.event_type = 'task.cancelled'
            ) THEN 'cancelled'
            WHEN EXISTS (
                SELECT 1 FROM runtime.task_events AS event
                WHERE event.task_id = runtime.tasks.id
                  AND event.event_type = '{CANCEL_REQUEST_EVENT}'
            ) AND status IN ('queued', 'retry_wait') THEN 'cancelled'
            WHEN EXISTS (
                SELECT 1 FROM runtime.task_events AS event
                WHERE event.task_id = runtime.tasks.id
                  AND event.event_type = '{CANCEL_REQUEST_EVENT}'
            ) AND status IN ('running', 'recovering', 'cancelling') THEN 'cancelling'
            ELSE status
        END
    """
    conditions = ["owner_user_id = $1::uuid"]
    args: list[Any] = [principal["id"]]
    if status is not None:
        args.append(status)
        conditions.append(f"({effective_status}) = ${len(args)}")
    if created_after is not None:
        args.append(created_after.astimezone(UTC))
        conditions.append(f"created_at >= ${len(args)}")
    if created_before is not None:
        args.append(created_before.astimezone(UTC))
        conditions.append(f"created_at < ${len(args)}")
    args.extend([limit + 1, offset])
    async with request.app.state.connect() as conn:
        rows = await conn.fetch(
            "SELECT id::text AS task_id, "
            f"({effective_status}) AS status, "
            "task_type, created_at, updated_at, version "
            "FROM runtime.tasks WHERE " + " AND ".join(conditions) +
            f" ORDER BY created_at DESC, id DESC LIMIT ${len(args)-1} OFFSET ${len(args)}",
            *args,
        )
    has_more = len(rows) > limit
    return {
        "items": [dict(row) for row in rows[:limit]],
        "limit": limit,
        "offset": offset,
        "has_more": has_more,
    }


def _event_cursor(value: str | None) -> int:
    if value is None:
        return 0
    if not re.fullmatch(r"[0-9]{1,19}", value):
        raise ApiError(400, "INVALID_REQUEST", "Last-Event-ID must be a nonnegative integer")
    cursor = int(value)
    if cursor > _MAX_EVENT_ID:
        raise ApiError(400, "INVALID_REQUEST", "Last-Event-ID is too large")
    return cursor


def _sse_frame(row: asyncpg.Record) -> bytes:
    payload = _json_value(row["payload_json"])
    data = _canonical(payload)
    event_type = str(row["event_type"]).replace("\r", "").replace("\n", "")
    return (
        f"id: {row['task_event_seq']}\nevent: {event_type}\ndata: {data}\n\n"
    ).encode("utf-8")


async def _task_event_stream(
    request: Request,
    task_id: str,
    cursor: int,
    *,
    user_id: str,
    token_version: int,
    token_expiry: int,
):
    """Replay persisted events and use LISTEN only to wake up the DB reader."""
    listener = None
    wakeup = asyncio.Event()
    next_reauthorization = 0.0

    def on_notification(_connection, _process_id, _channel, payload):
        try:
            notification = json.loads(payload)
            if notification.get("task_id") == task_id:
                wakeup.set()
        except (TypeError, ValueError, AttributeError):
            # Notifications are hints; malformed hints are ignored and polling
            # continues to read the durable event history.
            return

    def on_listener_termination(_connection):
        wakeup.set()

    try:
        while True:
            if int(datetime.now(UTC).timestamp()) >= token_expiry:
                break

            now_monotonic = asyncio.get_running_loop().time()
            if now_monotonic >= next_reauthorization:
                # No event rows are released after account, token, ownership, or
                # live grant revocation is observed. The same query below repeats
                # this check before reading each persisted batch.
                pass

            if listener is None or listener.is_closed():
                if listener is not None:
                    await listener.close()
                try:
                    listener = await asyncpg.connect(
                        request.app.state.settings.api_db_dsn,
                        timeout=_EVENT_DB_TIMEOUT_SECONDS,
                        server_settings={
                            "application_name": "agentforge-sse-listener-starting",
                            "statement_timeout": "1000",
                        },
                    )
                    await listener.add_listener(_EVENT_CHANNEL, on_notification)
                    await listener.execute("SET application_name = 'agentforge-sse-listener'")
                    listener.add_termination_listener(on_listener_termination)
                except _TRANSIENT_EVENT_DB_ERRORS:
                    if listener is not None:
                        await listener.close()
                    listener = None

            wakeup.clear()
            try:
                connect_events = getattr(
                    request.app.state,
                    "connect_events",
                    request.app.state.connect,
                )
                async with connect_events() as conn:
                    await conn.execute("SET statement_timeout = 900")
                    rows = await conn.fetch(
                        """
                        SELECT event.task_event_seq, event.event_type, event.payload_json,
                               task.owner_user_id = $2::uuid AS owns_task,
                               user_row.status = 'active' AS user_active,
                               user_row.token_version = $3 AS token_current,
                               NOT EXISTS (
                                   SELECT 1
                                   FROM jsonb_array_elements_text(task.region_scope_json)
                                        AS region(region_code)
                                   WHERE NOT EXISTS (
                                       SELECT 1 FROM identity.region_grants AS grant_row
                                       WHERE grant_row.region_code = region.region_code
                                         AND grant_row.user_id = task.owner_user_id
                                         AND grant_row.revoked_at IS NULL
                                         AND grant_row.permission IN ('read', 'admin')
                                   )
                               ) AS has_region_access
                        FROM runtime.tasks AS task
                        JOIN identity.users AS user_row ON user_row.id = $2::uuid
                        LEFT JOIN runtime.task_events AS event
                          ON event.task_id = task.id AND event.task_event_seq > $4
                        WHERE task.id = $1::uuid
                        ORDER BY event.task_event_seq
                        LIMIT 100
                        """,
                        task_id,
                        user_id,
                        token_version,
                        cursor,
                    )
            except _TRANSIENT_EVENT_DB_ERRORS:
                # A temporary database failure must not end the client stream.
                if listener is not None:
                    await listener.close()
                    listener = None
                wakeup.clear()
                try:
                    await asyncio.wait_for(wakeup.wait(), timeout=1.0)
                except TimeoutError:
                    pass
                yield b": keep-alive\n\n"
                continue

            if not rows or not all(
                rows[0][field]
                for field in ("owns_task", "user_active", "token_current", "has_region_access")
            ):
                break

            event_rows = [row for row in rows if row["task_event_seq"] is not None]
            if event_rows:
                for row in event_rows:
                    seq = row["task_event_seq"]
                    if seq <= cursor:
                        continue
                    cursor = seq
                    yield _sse_frame(row)
                continue

            poll_seconds = min(
                _EVENT_POLL_SECONDS,
                max(0.01, float(getattr(request.app.state, "event_poll_seconds", _EVENT_POLL_SECONDS))),
            )
            wait_seconds = min(poll_seconds, 1.0) if listener is None else poll_seconds
            try:
                await asyncio.wait_for(wakeup.wait(), timeout=wait_seconds)
            except TimeoutError:
                pass
            # Heartbeat comments carry no sequence and are safe to ignore.
            yield b": keep-alive\n\n"
    finally:
        if listener is not None:
            await listener.close()


@router.get("/{task_id}/events")
async def stream_task_events(
    task_id: uuid.UUID,
    request: Request,
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    credentials: HTTPAuthorizationCredentials | None = Depends(_SSE_BEARER),
    principal: dict[str, Any] = Depends(current_principal),
):
    async with request.app.state.connect() as conn:
        owns_task = await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM runtime.tasks WHERE id = $1::uuid AND owner_user_id = $2::uuid)",
            str(task_id),
            principal["id"],
        )
    if not owns_task:
        raise ApiError(404, "TASK_NOT_FOUND", "Task not found")
    cursor = _event_cursor(last_event_id)
    try:
        claims = jwt.decode(
            credentials.credentials if credentials is not None else "",
            request.app.state.settings.jwt_secret,
            algorithms=["HS256"],
            options={"require": ["exp", "token_version"]},
        )
        token_version = int(claims["token_version"])
        token_expiry = int(claims["exp"])
    except (jwt.InvalidTokenError, KeyError, TypeError, ValueError):
        raise ApiError(401, "UNAUTHENTICATED", "Authentication required") from None

    limiter = request.app.state.stream_limiter
    if not limiter.try_acquire(principal["id"]):
        raise ApiError(
            429,
            "RATE_LIMITED",
            "Too many active event streams",
            retryable=True,
            headers={"Retry-After": "1"},
        )

    async def stream_body():
        try:
            async for frame in _task_event_stream(
                request,
                str(task_id),
                cursor,
                user_id=principal["id"],
                token_version=token_version,
                token_expiry=token_expiry,
            ):
                yield frame
        finally:
            limiter.release(principal["id"])

    return StreamingResponse(
        stream_body(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/{task_id}")
async def get_task(
    task_id: uuid.UUID,
    request: Request,
    principal: dict[str, Any] = Depends(current_principal),
):
    async with request.app.state.connect() as conn:
        row = await conn.fetchrow(
            """
            SELECT task.id::text AS task_id, task.task_type, task.status,
                   task.version, task.current_attempt_no, task.result_json,
                   task.region_scope_json,
                   task.error_code, task.error_message, task.created_at,
                   NOT EXISTS (
                       SELECT 1
                       FROM jsonb_array_elements_text(task.region_scope_json)
                            AS scope(region_code)
                       WHERE NOT EXISTS (
                           SELECT 1 FROM identity.region_grants AS grant_row
                           WHERE grant_row.user_id = task.owner_user_id
                             AND grant_row.region_code = scope.region_code
                             AND grant_row.revoked_at IS NULL
                             AND grant_row.permission IN ('read', 'admin')
                       )
                   ) AS live_region_access,
                   task.updated_at, task.completed_at,
                   EXISTS (
                       SELECT 1 FROM runtime.task_events AS event
                       WHERE event.task_id = task.id
                         AND event.event_type = $3
                   ) AS cancel_requested,
                   EXISTS (
                       SELECT 1 FROM runtime.task_events AS event
                       WHERE event.task_id = task.id
                         AND event.event_type = 'task.cancelled'
                   ) AS cancel_committed
            FROM runtime.tasks AS task
            WHERE task.id = $1::uuid AND task.owner_user_id = $2::uuid
            """,
            str(task_id),
            principal["id"],
            CANCEL_REQUEST_EVENT,
        )
    if row is None:
        raise ApiError(404, "TASK_NOT_FOUND", "Task not found")
    result = dict(row)
    live_region_access = result["live_region_access"]
    stored_result = _json_value(result["result_json"])
    if not live_region_access:
        # Authorization snapshots document the grant at creation time only.
        # Stored result and error details remain available only while every
        # task region has a live read/admin grant for the owner.
        stored_result = None
    # The request event is durable immediately; the Worker state machine
    # materializes the status column shortly after. Project the externally
    # visible state so clients never observe a cancelled Task as runnable.
    status = result["status"]
    if result["cancel_committed"]:
        status = "cancelled"
    elif result["cancel_requested"]:
        if status in {"queued", "retry_wait"}:
            status = "cancelled"
        elif status in {"running", "recovering", "cancelling"}:
            status = "cancelling"
    return {
        "task_id": result["task_id"],
        "task_type": result["task_type"],
        "status": status,
        "cancel_requested": result["cancel_requested"],
        "version": result["version"],
        "current_attempt_no": result["current_attempt_no"],
        "progress": {"current_step": None, "completed_steps": 0, "total_steps": 4},
        "result": stored_result,
        "error": (
            {"code": result["error_code"], "message": result["error_message"]}
            if live_region_access
            and (
                result["error_code"] is not None
                or result["error_message"] is not None
            )
            else None
        ),
        "created_at": result["created_at"],
        "updated_at": result["updated_at"],
        "completed_at": result["completed_at"],
    }
