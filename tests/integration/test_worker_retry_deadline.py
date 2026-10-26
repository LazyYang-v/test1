"""Retry backoff, attempt exhaustion, deadline termination, and recovery edges."""

from __future__ import annotations

import asyncio

import asyncpg
import pytest

from agentforge_harness.models import (
    FakeModel,
    FakeScript,
    ModelProviderUnavailableError,
)
from agentforge_runtime.errors import LeaseLostError, TaskNotRunningError
from agentforge_runtime.store import RuntimeStore
from agentforge_worker.config import WorkerSettings
from agentforge_worker.worker import Worker

from worker_support import (
    complete_dataset_load,
    create_task_via_api,
    fetch_attempts,
    fetch_events,
    fetch_steps,
    fetch_task,
    post_cancel_via_api,
    seed_agent,
    wait_until,
)

pytestmark = pytest.mark.integration

TERMINAL_EVENTS = {"task.completed", "task.failed", "task.cancelled"}


def _settings(db: dict, **overrides) -> WorkerSettings:
    values = {
        "worker_db_dsn": db["role_dsn"]("worker"),
        "worker_id": "worker-retry",
        "heartbeat_interval_seconds": 0.05,
        "lease_seconds": 5,
        "poll_interval_seconds": 0.02,
        "cancel_poll_interval_seconds": 0.02,
        "model_timeout_seconds": 10,
        "max_model_repairs": 2,
    }
    values.update(overrides)
    return WorkerSettings(**values)


async def _store(db: dict, **overrides) -> RuntimeStore:
    pool = await asyncpg.create_pool(
        db["role_dsn"]("worker"), min_size=1, max_size=5
    )
    return RuntimeStore(
        pool,
        worker_id=overrides.get("worker_id", "worker-retry"),
        lease_seconds=overrides.get("lease_seconds", 5),
    )


async def _worker(db: dict, model, **overrides) -> Worker:
    store = await _store(db, **overrides)
    return Worker(_settings(db, **overrides), store, model)


async def _prepared_task(db: dict, **kwargs) -> str:
    await seed_agent(db)
    await complete_dataset_load(db)
    return await create_task_via_api(db, **kwargs)


async def _make_due(db: dict, task_id: str) -> None:
    conn = await asyncpg.connect(db["role_dsn"]("migrator"))
    try:
        await conn.execute(
            "UPDATE runtime.tasks SET next_retry_at = now() - interval '1 second' "
            "WHERE id = $1::uuid",
            task_id,
        )
    finally:
        await conn.close()


async def _expire_lease(db: dict, task_id: str) -> None:
    conn = await asyncpg.connect(db["role_dsn"]("migrator"))
    try:
        await conn.execute(
            "UPDATE runtime.task_attempts "
            "SET lease_expires_at = now() - interval '1 second' "
            "WHERE task_id = $1::uuid AND status = 'running'",
            task_id,
        )
    finally:
        await conn.close()


async def _retry_delay_seconds(db: dict, task_id: str) -> float:
    conn = await asyncpg.connect(db["role_dsn"]("migrator"))
    try:
        return float(
            await conn.fetchval(
                "SELECT EXTRACT(EPOCH FROM (next_retry_at - now())) "
                "FROM runtime.tasks WHERE id = $1::uuid",
                task_id,
            )
        )
    finally:
        await conn.close()


async def test_retryable_provider_failure_schedules_jittered_retry_then_due_claim(
    migrated_db,
):
    db = migrated_db
    task_id = await _prepared_task(db, max_attempts=3)
    failing = FakeModel(
        scripts={
            "plan_metrics:0": [
                FakeScript(
                    content=None,
                    error=ModelProviderUnavailableError("provider unavailable"),
                )
            ]
        }
    )
    worker = await _worker(db, failing)
    try:
        assert await worker.run_once() is True
    finally:
        await worker.close()

    task = await fetch_task(db, task_id)
    assert task["status"] == "retry_wait"
    assert task["current_attempt_no"] == 1
    assert task["error_code"] == "MODEL_PROVIDER_UNAVAILABLE"
    assert task["next_retry_at"] is not None
    delay = await _retry_delay_seconds(db, task_id)
    assert 29.0 <= delay <= 40.0

    attempts = await fetch_attempts(db, task_id)
    assert len(attempts) == 1
    assert attempts[0]["status"] == "failed"
    assert attempts[0]["error_code"] == "MODEL_PROVIDER_UNAVAILABLE"
    assert attempts[0]["fencing_epoch"] == 1
    events = await fetch_events(db, task_id)
    assert [event["type"] for event in events] == [
        "task.created",
        "task.attempt.started",
        "step.failed",
        "task.retry_scheduled",
    ]

    # Not due: a ready worker must not claim it yet.
    early = await _worker(db, FakeModel())
    try:
        assert await early.run_once() is False
    finally:
        await early.close()
    assert (await fetch_task(db, task_id))["status"] == "retry_wait"

    # Due: a new claim creates Attempt 2 with a higher fencing epoch.
    await _make_due(db, task_id)
    later = await _worker(db, FakeModel())
    try:
        assert await later.run_once() is True
    finally:
        await later.close()

    task = await fetch_task(db, task_id)
    assert task["status"] == "completed"
    assert task["current_attempt_no"] == 2
    attempts = await fetch_attempts(db, task_id)
    assert [(a["attempt_no"], a["status"], a["fencing_epoch"]) for a in attempts] == [
        (1, "failed", 1),
        (2, "succeeded", 2),
    ]
    events = await fetch_events(db, task_id)
    assert [event["seq"] for event in events] == list(range(1, len(events) + 1))
    assert [event["type"] for event in events].count("task.completed") == 1
    assert "task.retry_scheduled" in [event["type"] for event in events]
    assert [event["type"] for event in events].count("task.attempt.started") == 2


async def test_max_attempts_exhausted_after_retryable_failures(migrated_db):
    db = migrated_db
    task_id = await _prepared_task(db, max_attempts=2)
    model = FakeModel(
        scripts={
            "plan_metrics:0": [
                FakeScript(
                    content=None,
                    error=ModelProviderUnavailableError("provider down 1"),
                ),
                FakeScript(
                    content=None,
                    error=ModelProviderUnavailableError("provider down 2"),
                ),
            ]
        }
    )
    worker = await _worker(db, model)
    try:
        assert await worker.run_once() is True
    finally:
        await worker.close()
    assert (await fetch_task(db, task_id))["status"] == "retry_wait"

    await _make_due(db, task_id)
    worker = await _worker(db, model)
    try:
        assert await worker.run_once() is True
    finally:
        await worker.close()

    task = await fetch_task(db, task_id)
    assert task["status"] == "failed"
    assert task["error_code"] == "MODEL_PROVIDER_UNAVAILABLE"
    assert task["current_attempt_no"] == 2
    attempts = await fetch_attempts(db, task_id)
    assert [(a["attempt_no"], a["status"]) for a in attempts] == [
        (1, "failed"),
        (2, "failed"),
    ]
    events = await fetch_events(db, task_id)
    assert [event["type"] for event in events].count("task.failed") == 1
    assert [event["type"] for event in events].count("task.retry_scheduled") == 1
    assert "task.completed" not in [event["type"] for event in events]

    # Exhausted terminal state cannot be claimed again.
    again = await _worker(db, FakeModel())
    try:
        assert await again.run_once() is False
    finally:
        await again.close()


async def test_retry_wait_cancel_is_terminal_and_skips_future_attempts(migrated_db):
    db = migrated_db
    task_id = await _prepared_task(db, max_attempts=3)
    model = FakeModel(
        scripts={
            "plan_metrics:0": [
                FakeScript(
                    content=None,
                    error=ModelProviderUnavailableError("provider unavailable"),
                )
            ]
        }
    )
    worker = await _worker(db, model)
    try:
        assert await worker.run_once() is True
    finally:
        await worker.close()
    task = await fetch_task(db, task_id)
    assert task["status"] == "retry_wait"

    response = await post_cancel_via_api(db, task_id, task["version"])
    assert response.status_code == 202
    assert response.json()["status"] == "cancelled"

    later = await _worker(db, FakeModel())
    try:
        counts = await later.reconcile_once()
        assert counts["cancelled_unclaimed"] == 1
        assert await later.run_once() is False
    finally:
        await later.close()

    task = await fetch_task(db, task_id)
    assert task["status"] == "cancelled"
    assert task["next_retry_at"] is None
    attempts = await fetch_attempts(db, task_id)
    assert [(a["attempt_no"], a["status"]) for a in attempts] == [(1, "failed")]
    events = await fetch_events(db, task_id)
    assert [event["type"] for event in events].count("task.cancelled") == 1
    assert "task.completed" not in [event["type"] for event in events]


async def test_non_retryable_output_exhaustion_does_not_enter_retry_wait(migrated_db):
    db = migrated_db
    task_id = await _prepared_task(db, max_attempts=3)
    model = FakeModel(
        scripts={
            "plan_metrics:0": [
                FakeScript(content="{broken"),
                FakeScript(content="{still broken"),
                FakeScript(content="{}"),
            ]
        }
    )
    worker = await _worker(db, model)
    try:
        assert await worker.run_once() is True
    finally:
        await worker.close()

    task = await fetch_task(db, task_id)
    assert task["status"] == "failed"
    assert task["error_code"] == "MODEL_OUTPUT_INVALID"
    assert task["current_attempt_no"] == 1
    assert task["next_retry_at"] is None
    events = await fetch_events(db, task_id)
    assert "task.retry_scheduled" not in [event["type"] for event in events]
    assert [event["type"] for event in events][-1] == "task.failed"
    assert len(model.requests) == 3


async def test_deadline_scanner_fails_expired_queued_task(migrated_db):
    db = migrated_db
    task_id = await _prepared_task(db)
    conn = await asyncpg.connect(db["role_dsn"]("migrator"))
    try:
        await conn.execute(
            "UPDATE runtime.tasks SET deadline_at = now() - interval '1 second' "
            "WHERE id = $1::uuid",
            task_id,
        )
    finally:
        await conn.close()

    worker = await _worker(db, FakeModel())
    try:
        counts = await worker.reconcile_once()
        assert counts["deadline_failures"] == 1
    finally:
        await worker.close()

    task = await fetch_task(db, task_id)
    assert task["status"] == "failed"
    assert task["error_code"] == "DEADLINE_EXCEEDED"
    assert task["completed_at"] is not None
    assert await fetch_attempts(db, task_id) == []
    events = await fetch_events(db, task_id)
    assert [event["type"] for event in events] == ["task.created", "task.failed"]
    assert events[-1]["payload"]["error_code"] == "DEADLINE_EXCEEDED"


async def test_deadline_is_enforced_at_worker_node_boundary(migrated_db):
    db = migrated_db
    task_id = await _prepared_task(db)
    barrier = asyncio.Event()
    model = FakeModel(barrier=barrier)
    worker = await _worker(db, model)
    run = asyncio.create_task(worker.run_once())
    try:
        await wait_until(lambda: len(model.requests) >= 1)
        conn = await asyncpg.connect(db["role_dsn"]("migrator"))
        try:
            await conn.execute(
                "UPDATE runtime.tasks SET deadline_at = now() - interval '1 second' "
                "WHERE id = $1::uuid",
                task_id,
            )
        finally:
            await conn.close()
        barrier.set()
        assert await asyncio.wait_for(run, timeout=15) is True
    finally:
        barrier.set()
        if not run.done():
            run.cancel()
        await asyncio.gather(run, return_exceptions=True)
        await worker.close()

    task = await fetch_task(db, task_id)
    assert task["status"] == "failed"
    assert task["error_code"] == "DEADLINE_EXCEEDED"
    attempts = await fetch_attempts(db, task_id)
    assert len(attempts) == 1
    assert attempts[0]["status"] == "failed"
    assert attempts[0]["error_code"] == "DEADLINE_EXCEEDED"
    events = await fetch_events(db, task_id)
    assert [event["type"] for event in events].count("task.failed") == 1
    assert "task.completed" not in [event["type"] for event in events]
    assert "task.cancelled" not in [event["type"] for event in events]


async def test_cancelling_with_lost_lease_goes_recovering_then_cancelled(migrated_db):
    db = migrated_db
    task_id = await _prepared_task(db, max_attempts=3)
    store = await _store(db, worker_id="worker-cancel-recovery")
    try:
        lease = await store.claim()
        assert lease is not None
        task = await fetch_task(db, task_id)
        response = await post_cancel_via_api(db, task_id, task["version"])
        assert response.status_code == 202
        assert response.json()["status"] == "cancelling"

        assert await store.mark_cancelling(lease) is True
        assert (await fetch_task(db, task_id))["status"] == "cancelling"

        await _expire_lease(db, task_id)
        await store.reconcile()
        task = await fetch_task(db, task_id)
        assert task["status"] == "recovering"
        attempts = await fetch_attempts(db, task_id)
        assert attempts[0]["status"] == "abandoned"

        await store.reconcile()
        task = await fetch_task(db, task_id)
        assert task["status"] == "cancelled"
        events = await fetch_events(db, task_id)
        assert [event["type"] for event in events] == [
            "task.created",
            "task.attempt.started",
            "task.cancel.requested",
            "task.cancelling",
            "task.recovering",
            "task.cancelled",
        ]
        assert [event["seq"] for event in events] == list(range(1, 7))
        assert [event["type"] for event in events].count("task.cancelled") == 1
    finally:
        await store.close()


async def test_recovering_requeues_when_attempts_remain(migrated_db):
    db = migrated_db
    task_id = await _prepared_task(db, max_attempts=3)
    store = await _store(db, worker_id="worker-requeue")
    try:
        lease = await store.claim()
        assert lease is not None
        await _expire_lease(db, task_id)
        await store.reconcile()
        assert (await fetch_task(db, task_id))["status"] == "recovering"

        await store.reconcile()
        task = await fetch_task(db, task_id)
        assert task["status"] == "queued"
        assert task["error_code"] is None

        second = await store.claim()
        assert second is not None
        assert second.attempt_no == 2
        assert second.fencing_epoch == 2
        attempts = await fetch_attempts(db, task_id)
        assert [(a["attempt_no"], a["fencing_epoch"]) for a in attempts] == [
            (1, 1),
            (2, 2),
        ]
        events = await fetch_events(db, task_id)
        assert [event["type"] for event in events] == [
            "task.created",
            "task.attempt.started",
            "task.recovering",
            "task.requeued",
            "task.attempt.started",
        ]
        assert [event["seq"] for event in events] == list(range(1, 6))
    finally:
        await store.close()


async def test_recovering_fails_when_attempts_are_exhausted(migrated_db):
    db = migrated_db
    task_id = await _prepared_task(db, max_attempts=1)
    store = await _store(db, worker_id="worker-recovery-exhausted")
    try:
        lease = await store.claim()
        assert lease is not None
        await _expire_lease(db, task_id)
        await store.reconcile()
        assert (await fetch_task(db, task_id))["status"] == "recovering"
        await store.reconcile()
    finally:
        await store.close()

    task = await fetch_task(db, task_id)
    assert task["status"] == "failed"
    assert task["error_code"] == "ATTEMPTS_EXHAUSTED"
    events = await fetch_events(db, task_id)
    assert [event["type"] for event in events] == [
        "task.created",
        "task.attempt.started",
        "task.recovering",
        "task.failed",
    ]
    assert [event["type"] for event in events].count("task.failed") == 1


async def test_stale_attempt_cannot_write_after_retry_or_cancel(migrated_db):
    db = migrated_db
    # Retry path: an old attempt is fenced after a retry_wait transition.
    retry_task_id = await _prepared_task(db, max_attempts=3)
    store = await _store(db, worker_id="worker-stale")
    try:
        old = await store.claim()
        assert old is not None
        outcome = await store.retry_task(
            old, "MODEL_TIMEOUT", "provider timed out", delay_seconds=30
        )
        assert outcome == "retry_wait"
        with pytest.raises(TaskNotRunningError):
            await store.complete_task(old, {"report_version": "1.0"})
        with pytest.raises(TaskNotRunningError):
            await store.confirm_step(
                old,
                step_key="plan_metrics:0",
                graph_node_key="plan_metrics",
                input_hash="a" * 64,
                result={"stale": True},
            )
        assert await fetch_steps(db, retry_task_id) == []
        assert (await fetch_task(db, retry_task_id))["status"] == "retry_wait"

        await _make_due(db, retry_task_id)
        new = await store.claim()
        assert new is not None and new.fencing_epoch == 2
        with pytest.raises(LeaseLostError):
            await store.complete_task(old, {"report_version": "1.0"})
        with pytest.raises(LeaseLostError):
            await store.confirm_step(
                old,
                step_key="plan_metrics:0",
                graph_node_key="plan_metrics",
                input_hash="a" * 64,
                result={"stale": True},
            )
        assert (await fetch_task(db, retry_task_id))["status"] == "running"
        assert await fetch_steps(db, retry_task_id) == []
        events = await fetch_events(db, retry_task_id)
        assert "task.completed" not in [event["type"] for event in events]
        assert [event["type"] for event in events].count("task.attempt.started") == 2
    finally:
        await store.close()


async def test_stale_attempt_cannot_write_after_cancel(migrated_db):
    db = migrated_db
    task_id = await _prepared_task(db, max_attempts=3)
    store = await _store(db, worker_id="worker-stale-cancel")
    try:
        lease = await store.claim()
        assert lease is not None
        task = await fetch_task(db, task_id)
        response = await post_cancel_via_api(db, task_id, task["version"])
        assert response.status_code == 202
        assert await store.finalize_cancellation(lease) is True

        with pytest.raises(TaskNotRunningError):
            await store.fail_task(lease, "MODEL_TIMEOUT", "late failure")
        with pytest.raises(TaskNotRunningError):
            await store.complete_task(lease, {"report_version": "1.0"})
    finally:
        await store.close()

    task = await fetch_task(db, task_id)
    assert task["status"] == "cancelled"
    assert task["error_code"] is None
    attempts = await fetch_attempts(db, task_id)
    assert attempts[0]["status"] == "abandoned"
    events = await fetch_events(db, task_id)
    assert [event["type"] for event in events].count("task.cancelled") == 1
    assert "task.failed" not in [event["type"] for event in events]
    assert "task.completed" not in [event["type"] for event in events]


async def test_cancel_and_completion_race_produces_exactly_one_terminal_state(
    migrated_db,
):
    db = migrated_db
    task_id = await _prepared_task(db, max_attempts=3)
    store = await _store(db, worker_id="worker-race")
    try:
        lease = await store.claim()
        assert lease is not None

        async def complete_side():
            try:
                await store.complete_task(lease, {"report_version": "1.0"})
            except TaskNotRunningError:
                return
            except LeaseLostError:
                return

        async def cancel_side():
            async with store.pool.acquire() as conn:
                async with conn.transaction():
                    await conn.fetchval(
                        "SELECT pg_advisory_xact_lock(hashtextextended($1::text, 0))",
                        task_id,
                    )
                    row = await conn.fetchrow(
                        """
                        SELECT status,
                               EXISTS (
                                   SELECT 1 FROM runtime.task_events AS event
                                   WHERE event.task_id = runtime.tasks.id
                                     AND event.event_type = 'task.cancel.requested'
                               ) AS cancel_requested
                        FROM runtime.tasks WHERE id = $1::uuid
                        """,
                        task_id,
                    )
                    if (
                        row["status"] in {"completed", "failed", "cancelled"}
                        or row["cancel_requested"]
                    ):
                        return
                    seq = await conn.fetchval(
                        "SELECT COALESCE(MAX(task_event_seq), 0) + 1 "
                        "FROM runtime.task_events WHERE task_id = $1::uuid",
                        task_id,
                    )
                    await conn.execute(
                        """
                        INSERT INTO runtime.task_events
                            (task_id, attempt_id, task_event_seq, event_type,
                             payload_json)
                        VALUES ($1::uuid, NULL, $2, 'task.cancel.requested',
                                $3::jsonb)
                        """,
                        task_id,
                        seq,
                        '{"reason": "race"}',
                    )
            try:
                await store.finalize_cancellation(lease)
            except LeaseLostError:
                await store.abandon_attempt(lease)

        await asyncio.gather(complete_side(), cancel_side())
    finally:
        await store.close()

    task = await fetch_task(db, task_id)
    assert task["status"] in {"completed", "cancelled"}
    events = await fetch_events(db, task_id)
    terminal = [event for event in events if event["type"] in TERMINAL_EVENTS]
    assert len(terminal) == 1
    assert terminal[0]["type"] == (
        "task.completed" if task["status"] == "completed" else "task.cancelled"
    )
    assert [event["seq"] for event in events] == list(range(1, len(events) + 1))
    attempts = await fetch_attempts(db, task_id)
    assert len(attempts) == 1
    if task["status"] == "completed":
        assert attempts[0]["status"] == "succeeded"
    else:
        assert attempts[0]["status"] == "abandoned"
        assert attempts[0]["error_code"] == "CANCELLED"
