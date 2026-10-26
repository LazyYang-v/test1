"""Fixed benchmark behavior through the public API and real runtime."""

from __future__ import annotations

import asyncio
import uuid

import asyncpg
import pytest
from httpx import ASGITransport, AsyncClient

from agentforge_api.app import create_app
from agentforge_api.config import Settings
from agentforge_harness.models import FakeModel
from agentforge_runtime.store import RuntimeStore
from agentforge_worker.config import WorkerSettings
from agentforge_worker.worker import Worker

from worker_support import (
    SNAPSHOT_ID,
    USER_ID,
    app_for,
    complete_dataset_load,
    seed_agent,
    token,
)

pytestmark = pytest.mark.integration


async def _settings(db, **overrides):
    values = {
        "api_db_dsn": db["role_dsn"]("api"),
        "jwt_secret": "integration-test-signing-secret-at-least-32-bytes",
    }
    values.update(overrides)
    return Settings(**values)


def _run_body(**overrides):
    body = {"agent_key": "sales-report", "dataset_key": "p0-core-business-analysis"}
    body.update(overrides)
    return body


def _headers(key="evaluation-run-001"):
    return {
        "Authorization": f"Bearer {token()}",
        "Idempotency-Key": key,
    }


async def _run_worker_once(db, model=None):
    pool = await asyncpg.create_pool(db["role_dsn"]("worker"), min_size=1, max_size=5)
    store = RuntimeStore(pool, worker_id="evaluation-worker", lease_seconds=5)
    settings = WorkerSettings(
        worker_db_dsn=db["role_dsn"]("worker"),
        business_ro_db_dsn=db["role_dsn"]("business_ro"),
        worker_id="evaluation-worker",
        heartbeat_interval_seconds=0.05,
        lease_seconds=5,
        poll_interval_seconds=0.05,
        model_timeout_seconds=10,
        max_model_repairs=2,
    )
    worker = Worker(settings, store, model or FakeModel(), fake_model=model or FakeModel())
    try:
        return await worker.run_once()
    finally:
        await worker.close()


async def test_public_api_runs_all_fixed_cases_as_hidden_svc_eval_tasks(migrated_db):
    await seed_agent(migrated_db)
    await complete_dataset_load(migrated_db)
    app = app_for(migrated_db)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        datasets = await client.get("/api/v1/evaluations/datasets", headers=_headers())
        created = await client.post(
            "/api/v1/evaluations/runs",
            json=_run_body(),
            headers=_headers(),
        )

    assert datasets.status_code == 200
    assert datasets.json()["items"][0]["data_snapshot_id"] == SNAPSHOT_ID
    assert datasets.json()["items"][0]["case_count"] == 10
    assert created.status_code == 202, created.text
    run_id = created.json()["eval_run_id"]
    assert created.json()["model_mode"] == "fake"

    for _ in range(10):
        assert await _run_worker_once(migrated_db) is True

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        result = await client.get(
            f"/api/v1/evaluations/runs/{run_id}", headers=_headers()
        )
        listing = await client.get("/api/v1/tasks", headers=_headers())

    assert result.status_code == 200, result.text
    payload = result.json()
    assert payload["status"] == "completed"
    assert payload["summary"] == {
        "total": 10,
        "passed": 10,
        "failed": 0,
        "pending": 0,
        "score": "1.0000",
    }
    cases = {item["case_key"]: item for item in payload["case_results"]}
    assert set(cases) == {f"BA-{index:02d}" for index in range(1, 11)}
    assert all(item["status"] == "passed" for item in cases.values())
    assert all(item["duration_ms"] >= 0 for item in cases.values())
    assert cases["BA-02"]["score"]["actual"]["change_rate"] == "-0.1800"
    assert cases["BA-03"]["score"]["actual"]["change_rate"] is None
    assert "ZERO_BASELINE" in cases["BA-03"]["score"]["checks"]
    assert cases["BA-04"]["score"]["actual"]["current_value"] == "0.00"
    assert cases["BA-05"]["score"]["actual"]["current_value"] == "73250.53"
    assert cases["BA-07"]["score"]["actual"]["current_value"] == "71410.41"
    assert cases["BA-09"]["score"]["checks"]["hypotheses_unverified"] is True
    assert cases["BA-10"]["score"]["checks"]["findings_reference_valid_evidence"] is True
    assert listing.status_code == 200
    assert listing.json()["items"] == []

    # The public task API keeps service-owned evaluation Tasks private to the
    # svc-eval owner, even when a run result exposes its task ID to the runner.
    task_id = cases["BA-02"]["task_id"]
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        hidden = await client.get(f"/api/v1/tasks/{task_id}", headers=_headers())
    assert hidden.status_code == 404

    conn = await asyncpg.connect(migrated_db["role_dsn"]("migrator"))
    try:
        owner_email = await conn.fetchval(
            "SELECT email FROM identity.users WHERE id = "
            "(SELECT owner_user_id FROM runtime.tasks WHERE id = $1::uuid)",
            task_id,
        )
    finally:
        await conn.close()
    assert owner_email == "svc-eval@agentforge.internal"


async def test_live_evaluation_requires_explicit_enablement_and_call_budget(migrated_db):
    await seed_agent(migrated_db)
    await complete_dataset_load(migrated_db)
    default_app = app_for(migrated_db)

    async with AsyncClient(
        transport=ASGITransport(app=default_app), base_url="http://testserver"
    ) as client:
        disabled = await client.post(
            "/api/v1/evaluations/runs",
            json=_run_body(model_mode="live", live_call_budget=2),
            headers=_headers("live-disabled-001"),
        )

    assert disabled.status_code == 403
    assert disabled.json()["error"]["code"] == "LIVE_EVALUATIONS_DISABLED"

    settings = await _settings(
        migrated_db,
        live_evaluations_enabled=True,
        live_evaluation_call_budget_limit=3,
    )
    enabled_app = create_app(settings)
    async with AsyncClient(
        transport=ASGITransport(app=enabled_app), base_url="http://testserver"
    ) as client:
        missing_budget = await client.post(
            "/api/v1/evaluations/runs",
            json=_run_body(model_mode="live"),
            headers=_headers("live-budget-missing-001"),
        )
        excessive_budget = await client.post(
            "/api/v1/evaluations/runs",
            json=_run_body(model_mode="live", live_call_budget=4),
            headers=_headers("live-budget-excessive-001"),
        )

    assert missing_budget.status_code == 400
    assert missing_budget.json()["error"]["code"] == "INVALID_REQUEST"
    assert excessive_budget.status_code == 400
    assert excessive_budget.json()["error"]["code"] == "INVALID_REQUEST"


async def test_case_bound_to_another_snapshot_fails_without_creating_a_task(migrated_db):
    await seed_agent(migrated_db)
    await complete_dataset_load(migrated_db)
    conn = await asyncpg.connect(migrated_db["role_dsn"]("migrator"))
    try:
        await conn.execute(
            """
            INSERT INTO business.data_snapshots
                (data_snapshot_id, generator_version, seed, order_count,
                 canonicalization, sort_order)
            VALUES ($1, 'mismatch-fixture', 1, 1, 'json-sort-keys-compact-utf8',
                    'order_no,record_type')
            """,
            "0" * 64,
        )
        await conn.execute(
            """
            INSERT INTO evaluation.cases
                (id, dataset_id, case_key, input_json, expected_json,
                 assertions_json, data_snapshot_ref)
            SELECT $1::uuid, dataset.id, 'BA-99', base.input_json,
                   base.expected_json, base.assertions_json, $2
            FROM evaluation.datasets AS dataset
            CROSS JOIN evaluation.cases AS base
            WHERE dataset.dataset_key = 'p0-core-business-analysis'
              AND base.dataset_id = dataset.id AND base.case_key = 'BA-02'
            """,
            str(uuid.uuid4()),
            "0" * 64,
        )
    finally:
        await conn.close()

    async with AsyncClient(
        transport=ASGITransport(app=app_for(migrated_db)), base_url="http://testserver"
    ) as client:
        created = await client.post(
            "/api/v1/evaluations/runs",
            json=_run_body(),
            headers=_headers("evaluation-snapshot-mismatch-001"),
        )
        result = await client.get(
            f"/api/v1/evaluations/runs/{created.json()['eval_run_id']}",
            headers=_headers("evaluation-snapshot-mismatch-001"),
        )

    assert created.status_code == 202
    assert result.status_code == 200
    mismatched = next(
        item for item in result.json()["case_results"] if item["case_key"] == "BA-99"
    )
    assert mismatched["status"] == "failed"
    assert mismatched["failure_codes"] == ["DATA_SNAPSHOT_MISMATCH"]
    assert mismatched["task_id"] is None
