"""Claiming, leases, heartbeat, fencing, and lock discipline against real PostgreSQL."""

from __future__ import annotations

import asyncio
import dataclasses
from decimal import Decimal

import asyncpg
import pytest

from agentforge_harness.models import FakeModel
from agentforge_runtime.errors import LeaseLostError
from agentforge_runtime.store import RuntimeStore
from agentforge_worker.config import WorkerSettings
from agentforge_worker.worker import Worker
from test_api_tasks import _open_asgi_stream, _wait_for_sse_listener

from worker_support import (
    complete_dataset_load,
    create_task_via_api,
    fetch_attempts,
    fetch_events,
    fetch_evidences,
    fetch_steps,
    fetch_task,
    seed_agent,
    wait_until,
    app_for,
    token,
)

pytestmark = pytest.mark.integration


async def _pool(db: dict, *, size: int = 5) -> asyncpg.Pool:
    return await asyncpg.create_pool(
        db["role_dsn"]("worker"), min_size=1, max_size=size
    )


def _settings(db: dict, **overrides) -> WorkerSettings:
    values = {
        "worker_db_dsn": db["role_dsn"]("worker"),
        "business_ro_db_dsn": db["role_dsn"]("business_ro"),
        "worker_id": "worker-test",
        "heartbeat_interval_seconds": 0.05,
        "lease_seconds": 0.6,
        "poll_interval_seconds": 0.05,
        "model_timeout_seconds": 10,
        "max_model_repairs": 2,
    }
    values.update(overrides)
    return WorkerSettings(**values)


async def _prepared_task(db: dict, **seed_kwargs) -> str:
    await seed_agent(db, **seed_kwargs)
    await complete_dataset_load(db)
    return await create_task_via_api(db)


async def test_worker_event_is_delivered_to_live_sse_subscriber(migrated_db):
    db = migrated_db
    task_id = await _prepared_task(db)
    app = app_for(db)
    app.state.event_poll_seconds = 10
    stream = await _open_asgi_stream(
        app,
        f"/api/v1/tasks/{task_id}/events",
        {"Authorization": f"Bearer {token()}", "Last-Event-ID": "1"},
    )
    await _wait_for_sse_listener(db)
    pool = await _pool(db)
    store = RuntimeStore(pool, worker_id="worker-sse", lease_seconds=30)
    try:
        lease = await store.claim()
        assert lease is not None
        await asyncio.wait_for(stream["wait_for_events"](1), timeout=2)
        assert b'id: 2\nevent: task.attempt.started\ndata: ' in stream["body"]()
        assert b'"attempt_no":1' in stream["body"]()
    finally:
        await store.close()
        await stream["close"]()


async def test_two_concurrent_workers_claim_exactly_one_attempt(migrated_db):
    db = migrated_db
    task_id = await _prepared_task(db)
    pool_a = await _pool(db)
    pool_b = await _pool(db)
    store_a = RuntimeStore(pool_a, worker_id="worker-a", lease_seconds=30)
    store_b = RuntimeStore(pool_b, worker_id="worker-b", lease_seconds=30)
    try:
        first, second = await asyncio.gather(store_a.claim(), store_b.claim())
    finally:
        await store_a.close()
        await store_b.close()

    leases = [lease for lease in (first, second) if lease is not None]
    assert len(leases) == 1
    lease = leases[0]
    assert lease.task_id == task_id
    assert lease.attempt_no == 1
    assert lease.fencing_epoch == 1
    assert lease.worker_id in {"worker-a", "worker-b"}

    task = await fetch_task(db, task_id)
    assert task["status"] == "running"
    assert task["current_attempt_no"] == 1
    attempts = await fetch_attempts(db, task_id)
    assert len(attempts) == 1
    assert attempts[0]["status"] == "running"
    assert attempts[0]["fencing_epoch"] == 1
    assert attempts[0]["lease_token"] == lease.lease_token
    events = await fetch_events(db, task_id)
    assert [event["type"] for event in events] == [
        "task.created",
        "task.attempt.started",
    ]
    assert [event["seq"] for event in events] == [1, 2]
    assert events[1]["payload"]["attempt_no"] == 1
    assert events[1]["attempt_id"] == attempts[0]["id"]


async def test_heartbeat_renews_lease_while_model_call_is_blocked(migrated_db):
    db = migrated_db
    task_id = await _prepared_task(db)
    barrier = asyncio.Event()
    model = FakeModel(barrier=barrier)
    pool = await _pool(db)
    store = RuntimeStore(pool, worker_id="worker-heartbeat", lease_seconds=0.6)
    worker = Worker(
        _settings(db, worker_id="worker-heartbeat", lease_seconds=0.6),
        store,
        model,
    )
    run = asyncio.create_task(worker.run_once())
    try:
        # The claim committed and the plan model call is now blocked.
        await wait_until(lambda: len(model.requests) >= 1)
        attempts = await fetch_attempts(db, task_id)
        assert attempts[0]["status"] == "running"
        initial_heartbeat = attempts[0]["heartbeat_at"]

        # Stay blocked for longer than the original lease (0.6s).
        await asyncio.sleep(0.9)
        attempts = await fetch_attempts(db, task_id)
        assert attempts[0]["status"] == "running"
        assert attempts[0]["heartbeat_at"] > initial_heartbeat
        conn = await asyncpg.connect(db["role_dsn"]("migrator"))
        try:
            still_valid = await conn.fetchval(
                "SELECT lease_expires_at > now() FROM runtime.task_attempts "
                "WHERE task_id = $1::uuid",
                task_id,
            )
        finally:
            await conn.close()
        assert still_valid is True

        barrier.set()
        assert await asyncio.wait_for(run, timeout=15) is True
    finally:
        barrier.set()
        if not run.done():
            run.cancel()
        await asyncio.gather(run, return_exceptions=True)
        await worker.close()

    task = await fetch_task(db, task_id)
    assert task["status"] == "completed"
    attempts = await fetch_attempts(db, task_id)
    assert attempts[0]["status"] == "succeeded"


async def test_no_task_row_lock_is_held_during_model_execution(migrated_db):
    db = migrated_db
    task_id = await _prepared_task(db)
    barrier = asyncio.Event()
    model = FakeModel(barrier=barrier)
    pool = await _pool(db)
    store = RuntimeStore(pool, worker_id="worker-lock", lease_seconds=30)
    worker = Worker(_settings(db), store, model)
    run = asyncio.create_task(worker.run_once())
    observer = await asyncpg.connect(db["superuser_dsn"])
    try:
        await wait_until(lambda: len(model.requests) >= 1)
        tuple_locks = await observer.fetchval(
            """
            SELECT count(*)
            FROM pg_locks AS lock
            JOIN pg_stat_activity AS activity ON activity.pid = lock.pid
            WHERE lock.locktype = 'tuple'
              AND lock.relation = 'runtime.tasks'::regclass
              AND activity.usename = 'agentforge_worker'
            """
        )
        idle_in_transaction = await observer.fetchval(
            "SELECT count(*) FROM pg_stat_activity "
            "WHERE usename = 'agentforge_worker' AND state = 'idle in transaction'"
        )
        assert tuple_locks == 0
        assert idle_in_transaction == 0
        # A different session can lock the Task row immediately with NOWAIT.
        locked = await asyncio.wait_for(
            observer.fetchval(
                "SELECT id FROM runtime.tasks WHERE id = $1::uuid FOR UPDATE NOWAIT",
                task_id,
            ),
            timeout=5,
        )
        assert str(locked) == task_id
        await observer.execute("ROLLBACK")
        barrier.set()
        assert await asyncio.wait_for(run, timeout=15) is True
    finally:
        barrier.set()
        await observer.close()
        if not run.done():
            run.cancel()
        await asyncio.gather(run, return_exceptions=True)
        await worker.close()


async def test_stale_or_fenced_confirmation_is_rejected(migrated_db):
    db = migrated_db
    task_id = await _prepared_task(db)
    pool = await _pool(db)
    store = RuntimeStore(pool, worker_id="worker-stale", lease_seconds=30)
    try:
        lease = await store.claim()
        assert lease is not None
        conn = await asyncpg.connect(db["role_dsn"]("migrator"))
        try:
            await conn.execute(
                "UPDATE runtime.task_attempts SET lease_expires_at = now() - interval '1 second' "
                "WHERE id = $1::uuid",
                lease.attempt_id,
            )
        finally:
            await conn.close()
        with pytest.raises(LeaseLostError):
            await store.confirm_step(
                lease,
                step_key="plan_metrics:0",
                graph_node_key="plan_metrics",
                input_hash="a" * 64,
                result={"stale": True},
            )
        assert await store.lease_is_valid(lease) is False

        # A mismatched fencing epoch is equally rejected.
        stale_epoch = dataclasses.replace(lease, fencing_epoch=lease.fencing_epoch + 1)
        with pytest.raises(LeaseLostError):
            await store.confirm_step(
                stale_epoch,
                step_key="plan_metrics:0",
                graph_node_key="plan_metrics",
                input_hash="a" * 64,
                result={"stale": True},
            )
    finally:
        await store.close()
    assert await fetch_steps(db, task_id) == []
    assert [event["type"] for event in await fetch_events(db, task_id)] == [
        "task.created",
        "task.attempt.started",
    ]


async def test_replay_memoization_skips_confirmed_model_step(migrated_db):
    db = migrated_db
    task_id = await _prepared_task(db)
    pool = await _pool(db)
    store = RuntimeStore(pool, worker_id="worker-replay", lease_seconds=30)
    try:
        lease = await store.claim()
        assert lease is not None
        cached_plan = {
            "metric": "gmv",
            "metric_version": 1,
            "dimensions": ["region_code"],
            "filters": {"region_code": ["CN_EAST"]},
            "time_range": {
                "start": "2025-03-31",
                "end_exclusive": "2025-04-07",
                "timezone": "UTC",
            },
            "comparison": "previous_period",
            "limit": 100,
        }
        await store.confirm_step(
            lease,
            step_key="plan_metrics:0",
            graph_node_key="plan_metrics",
            input_hash="a" * 64,
            result=cached_plan,
        )
    finally:
        await store.close()

    model = FakeModel()
    pool = await _pool(db)
    store = RuntimeStore(pool, worker_id="worker-replay", lease_seconds=30)
    worker = Worker(_settings(db), store, model)
    try:
        # There is no second queued task; run the graph against the claimed attempt
        # through the worker's attempt runner to prove the confirmed step is reused.
        lease_row = (await fetch_attempts(db, task_id))[0]
        from agentforge_runtime.store import AttemptLease

        lease = AttemptLease(
            attempt_id=lease_row["id"],
            task_id=task_id,
            attempt_no=lease_row["attempt_no"],
            worker_id=lease_row["worker_id"],
            lease_token=lease_row["lease_token"],
            fencing_epoch=lease_row["fencing_epoch"],
        )
        await worker.run_attempt(lease)
    finally:
        await worker.close()

    assert await fetch_task(db, task_id) is not None
    # Compose is the only remaining model step; the cached plan was not re-requested.
    assert [request.metadata["step_key"] for request in model.requests] == [
        "compose_analysis:0"
    ]
    steps = {step["step_key"]: step for step in await fetch_steps(db, task_id)}
    assert set(steps) == {
        "plan_metrics:0",
        "execute_metrics:0",
        "compose_analysis:0",
        "validate_report:0",
    }
    assert all(step["status"] == "confirmed" for step in steps.values())
    assert steps["plan_metrics:0"]["result"] == cached_plan
    evidence = await fetch_evidences(db, task_id)
    assert evidence[0]["current_value"] == Decimal("57400.00")
    assert evidence[0]["previous_value"] == Decimal("70000.00")
    assert evidence[0]["change_rate"] == Decimal("-0.18000000")


async def test_claimed_lease_uses_database_time_and_monotonic_epoch(migrated_db):
    db = migrated_db
    await seed_agent(db)
    await complete_dataset_load(db)
    task_id = await create_task_via_api(db, idempotency_key="lease-time-task")
    pool = await _pool(db)
    store = RuntimeStore(pool, worker_id="worker-time", lease_seconds=5)
    try:
        lease = await store.claim()
        assert lease is not None
        conn = await asyncpg.connect(db["role_dsn"]("migrator"))
        try:
            row = await conn.fetchrow(
                "SELECT lease_expires_at > now() AS valid, "
                "heartbeat_at >= started_at AS beat, fencing_epoch "
                "FROM runtime.task_attempts WHERE id = $1::uuid",
                lease.attempt_id,
            )
        finally:
            await conn.close()
        assert row["valid"] is True
        assert row["beat"] is True
        assert row["fencing_epoch"] == 1
    finally:
        await store.close()
