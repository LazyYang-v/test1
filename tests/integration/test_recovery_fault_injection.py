"""Deterministic API and Worker crash-window recovery proofs.

These tests are intentionally PostgreSQL integration tests. They are authored
for the parent-coordinated database slot and are not run during ticket work.
"""

from __future__ import annotations

import asyncio
import uuid

import asyncpg
import pytest
from httpx import ASGITransport, AsyncClient

from agentforge_core.faults import FaultPoint, InjectedCrash
from agentforge_harness.models import FakeModel
from agentforge_runtime.store import RuntimeStore
from agentforge_worker.config import WorkerSettings
from agentforge_worker.worker import Worker

from worker_support import (
    app_for,
    complete_dataset_load,
    create_task_body,
    fetch_attempts,
    fetch_events,
    fetch_steps,
    fetch_task,
    seed_agent,
    token,
)

pytestmark = pytest.mark.integration


class CrashAt:
    def __init__(self, point: FaultPoint) -> None:
        self.point = point
        self.fired = False

    async def checkpoint(self, point, context) -> None:
        del context
        if point == self.point and not self.fired:
            self.fired = True
            raise InjectedCrash(f"injected crash at {self.point.value}")


def _settings(db: dict, **overrides) -> WorkerSettings:
    values = {
        "worker_db_dsn": db["role_dsn"]("worker"),
        "business_ro_db_dsn": db["role_dsn"]("business_ro"),
        "worker_id": "worker-recovery-fault",
        "heartbeat_interval_seconds": 0.05,
        "lease_seconds": 5,
        "poll_interval_seconds": 0.05,
        "model_timeout_seconds": 10,
        "max_model_repairs": 2,
    }
    values.update(overrides)
    return WorkerSettings(**values)


async def _prepared_task(db: dict, *, max_attempts: int = 3) -> str:
    await seed_agent(db)
    await complete_dataset_load(db)
    headers = {
        "Authorization": f"Bearer {token()}",
        "Idempotency-Key": f"recovery-{uuid.uuid4()}",
    }
    async with AsyncClient(
        transport=ASGITransport(app=app_for(db)), base_url="http://testserver"
    ) as client:
        response = await client.post(
            "/api/v1/tasks",
            json=create_task_body(max_attempts=max_attempts),
            headers=headers,
        )
    assert response.status_code == 202, response.text
    return response.json()["task_id"]


async def _worker(db: dict, *, fault_injector=None, model=None) -> Worker:
    pool = await asyncpg.create_pool(
        db["role_dsn"]("worker"), min_size=1, max_size=5
    )
    store = RuntimeStore(
        pool,
        worker_id="worker-recovery-fault",
        lease_seconds=5,
        fault_injector=fault_injector,
    )
    return Worker(
        _settings(db), store, model or FakeModel(), fault_injector=fault_injector
    )


async def _expire_current_lease(db: dict, task_id: str) -> None:
    conn = await asyncpg.connect(db["role_dsn"]("migrator"))
    try:
        await conn.execute(
            "UPDATE runtime.task_attempts SET lease_expires_at = now() - interval '1 second' "
            "WHERE task_id = $1::uuid AND status = 'running'",
            task_id,
        )
    finally:
        await conn.close()


async def _advance_to_claimable(db: dict, task_id: str) -> None:
    store_pool = await asyncpg.create_pool(db["role_dsn"]("worker"))
    store = RuntimeStore(store_pool, worker_id="worker-recovery-scan")
    try:
        await store.reconcile()
        assert (await fetch_task(db, task_id))["status"] == "recovering"
        await store.reconcile()
        assert (await fetch_task(db, task_id))["status"] == "queued"
    finally:
        await store.close()


@pytest.mark.parametrize(
    "point",
    [
        FaultPoint.ATTEMPT_CLAIMED,
        FaultPoint.MODEL_CALL_IN_FLIGHT,
        FaultPoint.MODEL_RETURNED,
        FaultPoint.TOOL_RESULT_BEFORE_PERSIST,
        FaultPoint.TOOL_RESULT_BEFORE_COMMIT,
        FaultPoint.TOOL_RESULT_PERSISTED,
        FaultPoint.STEP_CONFIRM_BEFORE_COMMIT,
        FaultPoint.STEP_CONFIRMED,
        FaultPoint.GRAPH_CHECKPOINT_SAVED,
        FaultPoint.TERMINAL_BEFORE_COMMIT,
    ],
)
async def test_worker_crash_window_recovers_without_false_success(migrated_db, point):
    db = migrated_db
    task_id = await _prepared_task(db)
    injector = CrashAt(point)
    first_model = FakeModel()
    worker = await _worker(db, fault_injector=injector, model=first_model)
    try:
        with pytest.raises(InjectedCrash):
            await worker.run_once()
    finally:
        await worker.close()

    assert injector.fired is True
    task = await fetch_task(db, task_id)
    assert task["status"] == "running"
    assert task["result_json"] is None
    assert "task.completed" not in [event["type"] for event in await fetch_events(db, task_id)]

    await _expire_current_lease(db, task_id)
    await _advance_to_claimable(db, task_id)
    recovered_model = FakeModel()
    worker = await _worker(db, model=recovered_model)
    try:
        assert await worker.run_once() is True
    finally:
        await worker.close()

    task = await fetch_task(db, task_id)
    assert task["status"] == "completed"
    assert task["result_json"]["report_version"] == "1.0"
    assert [attempt["status"] for attempt in await fetch_attempts(db, task_id)] == [
        "abandoned",
        "succeeded",
    ]
    assert "task.completed" in [event["type"] for event in await fetch_events(db, task_id)]
    assert all(step["status"] == "confirmed" for step in await fetch_steps(db, task_id))

    # Confirmed steps and accepted tool calls are the replay source; a crash
    # after terminal commit preparation must not require a second model call.
    if point == FaultPoint.TERMINAL_BEFORE_COMMIT:
        assert recovered_model.requests == []


async def test_recovery_scan_fault_rolls_back_and_next_scan_advances(migrated_db):
    db = migrated_db
    task_id = await _prepared_task(db)
    worker = await _worker(db)
    try:
        assert await worker.run_once() is True
    finally:
        await worker.close()

    # Create a second Task to exercise expiry recovery independently.
    task_id = await _prepared_task(db)
    pool = await asyncpg.create_pool(db["role_dsn"]("worker"))
    store = RuntimeStore(pool, worker_id="worker-recovery-scan")
    lease = await store.claim()
    assert lease is not None and lease.task_id == task_id
    await store.close()
    await _expire_current_lease(db, task_id)
    pool = await asyncpg.create_pool(db["role_dsn"]("worker"))
    store = RuntimeStore(pool, worker_id="worker-recovery-scan")
    try:
        store.fault_injector = CrashAt(FaultPoint.RECOVERY_SCAN_BEFORE_COMMIT)
        with pytest.raises(InjectedCrash):
            await store.reconcile()
        assert (await fetch_task(db, task_id))["status"] == "recovering"
        store.fault_injector = None
        await store.reconcile()
        assert (await fetch_task(db, task_id))["status"] == "queued"
    finally:
        await store.close()


async def test_api_create_crash_before_and_after_commit_is_idempotent(migrated_db):
    db = migrated_db
    await seed_agent(db)
    await complete_dataset_load(db)
    body = create_task_body()
    key = f"api-crash-{uuid.uuid4()}"
    headers = {"Authorization": f"Bearer {token()}", "Idempotency-Key": key}

    app = app_for(db)
    app.state.fault_injector = CrashAt(FaultPoint.TASK_CREATE_BEFORE_COMMIT)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        with pytest.raises(InjectedCrash):
            await client.post("/api/v1/tasks", json=body, headers=headers)

    app.state.fault_injector = CrashAt(FaultPoint.TASK_CREATE_AFTER_COMMIT)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        with pytest.raises(InjectedCrash):
            await client.post("/api/v1/tasks", json=body, headers=headers)
        replay = await client.post("/api/v1/tasks", json=body, headers=headers)
    assert replay.status_code == 202
    assert replay.json()["status"] == "queued"
    assert replay.json()["task_id"]


async def test_inconsistent_confirmed_step_state_fails_closed(migrated_db):
    db = migrated_db
    task_id = await _prepared_task(db)
    pool = await asyncpg.create_pool(db["role_dsn"]("worker"))
    store = RuntimeStore(pool, worker_id="worker-bad-recovery", lease_seconds=5)
    lease = await store.claim()
    assert lease is not None
    await store.close()

    conn = await asyncpg.connect(db["role_dsn"]("migrator"))
    try:
        await conn.execute(
            "INSERT INTO runtime.step_executions "
            "(id, task_id, step_key, graph_node_key, input_hash, status, result_json, "
            "result_hash, owning_attempt_id, fencing_epoch, started_at, completed_at) "
            "VALUES ($1::uuid, $2::uuid, 'plan_metrics:0', 'plan_metrics', $3, "
            "'confirmed', '{\"metric\":\"gmv\"}'::jsonb, $4, $5::uuid, 1, now(), now())",
            str(uuid.uuid4()),
            task_id,
            "a" * 64,
            "0" * 64,
            lease.attempt_id,
        )
    finally:
        await conn.close()

    await _expire_current_lease(db, task_id)
    await _advance_to_claimable(db, task_id)
    task = await fetch_task(db, task_id)
    assert task["status"] == "failed"
    assert task["error_code"] == "RECOVERY_STATE_INVALID"
    assert "result hash" in task["error_message"]
    assert "task.completed" not in [event["type"] for event in await fetch_events(db, task_id)]


async def test_old_fencing_epoch_cannot_commit_terminal_step_or_tool_result(migrated_db):
    db = migrated_db
    task_id = await _prepared_task(db, max_attempts=3)
    pool = await asyncpg.create_pool(db["role_dsn"]("worker"))
    store = RuntimeStore(pool, worker_id="worker-fencing", lease_seconds=5)
    old = await store.claim()
    assert old is not None
    await _expire_current_lease(db, task_id)
    await store.reconcile()
    await store.reconcile()
    new = await store.claim()
    assert new is not None and new.fencing_epoch == old.fencing_epoch + 1

    from agentforge_runtime.errors import LeaseLostError

    with pytest.raises(LeaseLostError):
        await store.complete_task(old, {"report_version": "1.0"})
    with pytest.raises(LeaseLostError):
        await store.confirm_step(
            old,
            step_key="plan_metrics:0",
            graph_node_key="plan_metrics",
            input_hash="a" * 64,
            result={"metric": "gmv"},
        )
    with pytest.raises(LeaseLostError):
        await store.fail_task(old, "INTERNAL_ERROR", "stale failure")
    assert (await fetch_task(db, task_id))["status"] == "running"
    assert [event["type"] for event in await fetch_events(db, task_id)].count(
        "task.completed"
    ) == 0
    assert await fetch_steps(db, task_id) == []
    await store.close()
