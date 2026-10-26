"""Owner-scoped cancellation through HTTP, plus real-worker cooperation."""

from __future__ import annotations

import asyncio

import asyncpg
import pytest
from httpx import ASGITransport, AsyncClient

from agentforge_harness.models import FakeModel
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
    get_task_via_api,
    post_cancel_via_api,
    seed_agent,
    wait_until,
    app_for,
    token,
    USER_ID,
)

pytestmark = pytest.mark.integration

OTHER_USER_ID = "0192a3b4-c5d6-7e8f-9012-3456789abcdf"


def _settings(db: dict, **overrides) -> WorkerSettings:
    values = {
        "worker_db_dsn": db["role_dsn"]("worker"),
        "business_ro_db_dsn": db["role_dsn"]("business_ro"),
        "worker_id": "worker-cancel",
        "heartbeat_interval_seconds": 0.05,
        "lease_seconds": 5,
        "poll_interval_seconds": 0.02,
        "cancel_poll_interval_seconds": 0.02,
        "model_timeout_seconds": 10,
        "max_model_repairs": 2,
    }
    values.update(overrides)
    return WorkerSettings(**values)


async def _worker(db: dict, model: FakeModel, **overrides):
    pool = await asyncpg.create_pool(
        db["role_dsn"]("worker"), min_size=1, max_size=5
    )
    store = RuntimeStore(
        pool,
        worker_id=overrides.get("worker_id", "worker-cancel"),
        lease_seconds=overrides.get("lease_seconds", 5),
    )
    worker = Worker(_settings(db, **overrides), store, model)
    return worker


async def _prepared_task(db: dict, **kwargs) -> str:
    await seed_agent(db)
    await complete_dataset_load(db)
    return await create_task_via_api(db, **kwargs)


async def test_queued_cancel_is_terminal_with_one_contiguous_event_stream(migrated_db):
    db = migrated_db
    task_id = await _prepared_task(db)

    detail = await get_task_via_api(db, task_id)
    assert detail["status"] == "queued"
    assert detail["version"] == 1
    assert detail["cancel_requested"] is False

    response = await post_cancel_via_api(db, task_id, detail["version"])
    assert response.status_code == 202
    assert response.json()["status"] == "cancelled"
    assert response.json()["cancel_requested"] is True

    # The request event is durable immediately and the read model projects the
    # terminal state before the scanner materializes the status column.
    projected = await get_task_via_api(db, task_id)
    assert projected["status"] == "cancelled"
    assert projected["cancel_requested"] is True

    # A non-executing Task is cancelled immediately in the durable event log.
    immediate = await fetch_events(db, task_id)
    assert [event["type"] for event in immediate] == [
        "task.created",
        "task.cancel.requested",
        "task.cancelled",
    ]

    worker = await _worker(db, FakeModel())
    try:
        counts = await worker.reconcile_once()
        assert counts["cancelled_unclaimed"] == 1
        assert await worker.run_once() is False
    finally:
        await worker.close()

    task = await fetch_task(db, task_id)
    assert task["status"] == "cancelled"
    assert task["cancel_requested_at"] is not None
    assert task["completed_at"] is not None
    assert task["error_code"] is None
    assert await fetch_attempts(db, task_id) == []
    assert await fetch_steps(db, task_id) == []

    events = await fetch_events(db, task_id)
    assert [event["seq"] for event in events] == [1, 2, 3]
    assert [event["type"] for event in events] == [
        "task.created",
        "task.cancel.requested",
        "task.cancelled",
    ]
    assert events[1]["payload"]["reason"] == "operator requested cancellation"
    assert sum(event["type"] == "task.cancelled" for event in events) == 1


async def test_queued_cancel_version_conflict_is_409_without_side_effects(migrated_db):
    db = migrated_db
    task_id = await _prepared_task(db)

    response = await post_cancel_via_api(db, task_id, 99)
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "INVALID_STATE_TRANSITION"
    assert error["details"]["current_version"] == 1

    task = await fetch_task(db, task_id)
    assert task["status"] == "queued"
    assert task["cancel_requested_at"] is None
    assert [event["type"] for event in await fetch_events(db, task_id)] == [
        "task.created"
    ]


async def test_cancel_is_owner_scoped_and_foreign_tasks_are_404(migrated_db):
    db = migrated_db
    task_id = await _prepared_task(db)
    conn = await asyncpg.connect(db["role_dsn"]("migrator"))
    try:
        await conn.execute(
            "INSERT INTO identity.users (id, email, display_name, status) "
            "VALUES ($1::uuid, 'other-cancel@example.com', 'Other', 'active')",
            OTHER_USER_ID,
        )
    finally:
        await conn.close()

    response = await post_cancel_via_api(
        db, task_id, 1, user_id=OTHER_USER_ID
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "TASK_NOT_FOUND"
    assert [event["type"] for event in await fetch_events(db, task_id)] == [
        "task.created"
    ]


async def test_terminal_task_cancel_returns_409(migrated_db):
    db = migrated_db
    task_id = await _prepared_task(db)
    worker = await _worker(db, FakeModel())
    try:
        assert await worker.run_once() is True
    finally:
        await worker.close()
    completed = await get_task_via_api(db, task_id)
    assert completed["status"] == "completed"

    response = await post_cancel_via_api(db, task_id, completed["version"])
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "INVALID_STATE_TRANSITION"

    events = await fetch_events(db, task_id)
    assert [event["type"] for event in events].count("task.completed") == 1
    assert "task.cancel.requested" not in [event["type"] for event in events]


async def test_repeated_cancel_request_conflicts(migrated_db):
    db = migrated_db
    task_id = await _prepared_task(db)
    first = await post_cancel_via_api(db, task_id, 1)
    assert first.status_code == 202

    second = await post_cancel_via_api(db, task_id, 1)
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "INVALID_STATE_TRANSITION"


async def test_running_cancel_stops_worker_before_any_new_model_call(migrated_db):
    db = migrated_db
    task_id = await _prepared_task(db)
    barrier = asyncio.Event()
    model = FakeModel(barrier=barrier)
    worker = await _worker(db, model)
    run = asyncio.create_task(worker.run_once())
    try:
        # The plan model call is blocked at the barrier.
        await wait_until(lambda: len(model.requests) >= 1)
        running = await get_task_via_api(db, task_id)
        assert running["status"] == "running"
        assert running["version"] == 2

        response = await post_cancel_via_api(db, task_id, running["version"])
        assert response.status_code == 202
        assert response.json()["status"] == "cancelling"

        assert await asyncio.wait_for(run, timeout=15) is True
    finally:
        barrier.set()
        if not run.done():
            run.cancel()
        await asyncio.gather(run, return_exceptions=True)
        await worker.close()

    task = await fetch_task(db, task_id)
    assert task["status"] == "cancelled"
    assert task["cancel_requested_at"] is not None
    assert task["completed_at"] is not None

    attempts = await fetch_attempts(db, task_id)
    assert len(attempts) == 1
    assert attempts[0]["status"] == "abandoned"
    assert attempts[0]["error_code"] == "CANCELLED"

    # The blocked call was already in flight; no repair or subsequent node ran.
    assert len(model.requests) == 1
    assert await fetch_steps(db, task_id) == []

    events = await fetch_events(db, task_id)
    assert [event["seq"] for event in events] == list(range(1, len(events) + 1))
    assert [event["type"] for event in events] == [
        "task.created",
        "task.attempt.started",
        "task.cancel.requested",
        "task.cancelling",
        "task.cancelled",
    ]
    assert sum(event["type"] == "task.cancelled" for event in events) == 1

    # No later worker can claim the terminal Task.
    later = await _worker(db, FakeModel())
    try:
        assert await later.run_once() is False
    finally:
        await later.close()


async def test_completed_result_is_hidden_after_live_region_grant_revocation(migrated_db):
    db = migrated_db
    task_id = await _prepared_task(db)
    worker = await _worker(db, FakeModel())
    try:
        assert await worker.run_once() is True
    finally:
        await worker.close()

    completed = await get_task_via_api(db, task_id)
    assert completed["status"] == "completed"
    assert completed["result"] is not None

    conn = await asyncpg.connect(db["role_dsn"]("migrator"))
    try:
        await conn.execute(
            "UPDATE identity.region_grants SET revoked_at = now() "
            "WHERE user_id = $1::uuid AND region_code = 'CN_EAST' "
            "AND revoked_at IS NULL",
            USER_ID,
        )
    finally:
        await conn.close()

    async with AsyncClient(
        transport=ASGITransport(app=app_for(db)), base_url="http://testserver"
    ) as client:
        response = await client.get(
            f"/api/v1/tasks/{task_id}",
            headers={"Authorization": f"Bearer {token()}"},
        )

    # Keep the owner's task discoverable while redacting historical data whose
    # live authorization has been revoked, including potentially sensitive
    # error details.
    assert response.status_code == 200, response.text
    detail = response.json()
    assert detail["status"] == "completed"
    assert detail["result"] is None
    assert detail["error"] is None

    conn = await asyncpg.connect(db["role_dsn"]("migrator"))
    try:
        await conn.execute(
            "UPDATE runtime.tasks SET status = 'failed', "
            "error_code = 'REPORT_PRIVATE_DETAIL', "
            "error_message = 'sensitive regional value' WHERE id = $1::uuid",
            task_id,
        )
    finally:
        await conn.close()
    async with AsyncClient(
        transport=ASGITransport(app=app_for(db)), base_url="http://testserver"
    ) as client:
        failed_response = await client.get(
            f"/api/v1/tasks/{task_id}",
            headers={"Authorization": f"Bearer {token()}"},
        )
    assert failed_response.status_code == 200
    failed_detail = failed_response.json()
    assert failed_detail["status"] == "failed"
    assert failed_detail["result"] is None
    assert failed_detail["error"] is None


async def test_cancel_reason_is_required(migrated_db):
    db = migrated_db
    task_id = await _prepared_task(db)
    response = await post_cancel_via_api(db, task_id, 1, reason="   ")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_REQUEST"
