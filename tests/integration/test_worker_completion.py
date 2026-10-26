"""HTTP -> real worker -> PostgreSQL tracer bullet, including closed failure paths."""

from __future__ import annotations

import json
import uuid
from decimal import Decimal

import asyncpg
import pytest

from agentforge_harness.models import FakeModel, FakeScript
from agentforge_runtime.store import RuntimeStore
from agentforge_worker.config import WorkerSettings
from agentforge_worker.executors import MetricsQueryExecutor
from agentforge_worker.worker import Worker

from worker_support import (
    FROZEN_DATE_RANGE,
    SNAPSHOT_ID,
    complete_dataset_load,
    create_task_via_api,
    fetch_attempts,
    fetch_audits,
    fetch_evidences,
    fetch_events,
    fetch_steps,
    fetch_task,
    get_task_via_api,
    registry_cli,
    seed_agent,
)

pytestmark = pytest.mark.integration

STEP_KEYS = [
    "plan_metrics:0",
    "execute_metrics:0",
    "compose_analysis:0",
    "validate_report:0",
]
NODE_KEYS = [
    "plan_metrics",
    "execute_metrics",
    "compose_analysis",
    "validate_report",
]


def _settings(db: dict, **overrides) -> WorkerSettings:
    values = {
        "worker_db_dsn": db["role_dsn"]("worker"),
        "business_ro_db_dsn": db["role_dsn"]("business_ro"),
        "worker_id": "worker-completion",
        "heartbeat_interval_seconds": 0.05,
        "lease_seconds": 5,
        "poll_interval_seconds": 0.05,
        "model_timeout_seconds": 10,
        "max_model_repairs": 2,
    }
    values.update(overrides)
    return WorkerSettings(**values)


async def _run_worker(migrated_db, model: FakeModel, **overrides) -> bool:
    pool = await asyncpg.create_pool(
        migrated_db["role_dsn"]("worker"), min_size=1, max_size=5
    )
    store = RuntimeStore(
        pool,
        worker_id=overrides.get("worker_id", "worker-completion"),
        lease_seconds=overrides.get("lease_seconds", 5),
    )
    worker = Worker(_settings(migrated_db, **overrides), store, model)
    try:
        return await worker.run_once()
    finally:
        await worker.close()


async def _prepared_task(migrated_db, *, agent_key="sales-report", bind_tool=True):
    seeded = await seed_agent(
        migrated_db, agent_key=agent_key, bind_tool=bind_tool
    )
    await complete_dataset_load(migrated_db)
    task_id = await create_task_via_api(migrated_db, agent_key)
    return seeded, task_id


async def test_scripted_fake_runs_four_ordered_steps_to_a_valid_report(migrated_db):
    db = migrated_db
    seeded, task_id = await _prepared_task(db)
    model = FakeModel()

    assert await _run_worker(db, model) is True

    detail = await get_task_via_api(db, task_id)
    assert detail["status"] == "completed"
    assert detail["completed_at"] is not None
    assert detail["current_attempt_no"] == 1
    assert detail["error"] is None
    report = detail["result"]
    assert report["report_version"] == "1.0"
    assert report["summary"]
    assert report["metrics"] == [
        {
            "metric": "gmv",
            "metric_version": 1,
            "unit": "CNY",
            "current_value": "57400.00",
            "previous_value": "70000.00",
            "absolute_change": "-12600.00",
            "change_rate": "-0.1800",
            "evidence_id": report["evidence_ids"][0],
            "data_snapshot_id": SNAPSHOT_ID,
        }
    ]
    assert report["findings"][0]["evidence_ids"] == report["evidence_ids"]
    assert report["hypotheses"][0]["status"] == "unverified"
    assert report["limitations"]
    assert report["evidence_ids"] == [report["metrics"][0]["evidence_id"]]

    steps = {step["step_key"]: step for step in await fetch_steps(db, task_id)}
    assert set(steps) == set(STEP_KEYS)
    assert all(step["status"] == "confirmed" for step in steps.values())
    assert {step["graph_node_key"] for step in steps.values()} == set(NODE_KEYS)
    assert [step["step_key"] for step in steps.values()] == [
        steps[key]["step_key"] for key in STEP_KEYS
    ]
    assert steps["plan_metrics:0"]["result"]["metric"] == "gmv"
    assert steps["execute_metrics:0"]["result"]["evidence_id"] == report["evidence_ids"][0]
    assert steps["validate_report:0"]["result"] == report

    events = await fetch_events(db, task_id)
    assert [event["seq"] for event in events] == list(range(1, 8))
    completed_steps = [
        event["payload"]["step_key"]
        for event in events
        if event["type"] == "step.completed"
    ]
    assert completed_steps == STEP_KEYS
    assert [event["type"] for event in events] == [
        "task.created",
        "task.attempt.started",
        "step.completed",
        "step.completed",
        "step.completed",
        "step.completed",
        "task.completed",
    ]
    assert events[-1]["payload"]["report_version"] == "1.0"

    attempts = await fetch_attempts(db, task_id)
    assert len(attempts) == 1
    assert attempts[0]["status"] == "succeeded"
    assert attempts[0]["finished_at"] is not None
    assert attempts[0]["fencing_epoch"] == 1
    assert attempts[0]["worker_id"] == "worker-completion"

    evidence = await fetch_evidences(db, task_id)
    assert len(evidence) == 1
    assert evidence[0]["task_id"] == task_id
    assert evidence[0]["data_snapshot_id"].strip() == SNAPSHOT_ID
    assert evidence[0]["current_value"] == Decimal("57400.00")
    assert evidence[0]["previous_value"] == Decimal("70000.00")
    assert evidence[0]["absolute_change"] == Decimal("-12600.00")
    assert evidence[0]["change_rate"] == Decimal("-0.18000000")

    conn = await asyncpg.connect(db["role_dsn"]("migrator"))
    try:
        invocations = await conn.fetch(
            "SELECT invocation_key, status, input_tokens, output_tokens, trace_id, "
            "estimated_cost FROM harness.model_invocations WHERE task_id = $1::uuid "
            "ORDER BY started_at, invocation_key",
            task_id,
        )
        tools = await conn.fetch(
            "SELECT invocation_key, status, result_hash, trace_id FROM harness.tool_invocations "
            "WHERE task_id = $1::uuid",
            task_id,
        )
    finally:
        await conn.close()
    assert len(invocations) == 2
    assert all(row["status"] == "succeeded" for row in invocations)
    assert all(row["trace_id"] == task_id for row in invocations)
    assert all(row["input_tokens"] > 0 and row["output_tokens"] > 0 for row in invocations)
    assert all(row["estimated_cost"] > 0 for row in invocations)
    assert len(tools) == 1 and tools[0]["status"] == "succeeded"
    assert tools[0]["result_hash"]
    assert tools[0]["trace_id"] == task_id
    assert all(event["trace_id"] == task_id for event in events)

    audits = await fetch_audits(db, action="tool.invocation")
    assert len(audits) == 1
    assert audits[0]["decision"] == "allowed"
    assert audits[0]["trace_id"] == task_id
    assert audits[0]["metadata"]["invocation_key"] == tools[0]["invocation_key"]


async def test_invalid_json_exhausts_bounded_repair_and_fails_explicitly(migrated_db):
    db = migrated_db
    _, task_id = await _prepared_task(db)
    model = FakeModel(
        scripts={
            "plan_metrics:0": [
                FakeScript(content="{broken"),
                FakeScript(content="{still broken"),
                FakeScript(content="{}"),
            ]
        }
    )

    assert await _run_worker(db, model) is True

    task = await fetch_task(db, task_id)
    assert task["status"] == "failed"
    assert task["error_code"] == "MODEL_OUTPUT_INVALID"
    assert task["result_json"] is None
    assert await fetch_evidences(db, task_id) == []
    steps = await fetch_steps(db, task_id)
    assert [(step["step_key"], step["status"]) for step in steps] == [
        ("plan_metrics:0", "failed")
    ]
    attempts = await fetch_attempts(db, task_id)
    assert attempts[0]["status"] == "failed"
    assert attempts[0]["error_code"] == "MODEL_OUTPUT_INVALID"
    events = await fetch_events(db, task_id)
    assert [event["type"] for event in events] == [
        "task.created",
        "task.attempt.started",
        "step.failed",
        "task.failed",
    ]
    assert events[2]["payload"]["error_code"] == "MODEL_OUTPUT_INVALID"
    assert len(model.requests) == 3
    assert all("Repair" in request.messages[-1]["content"]
               for request in model.requests[1:])


async def test_unbound_tool_fails_closed_and_is_audited(migrated_db):
    db = migrated_db
    _, task_id = await _prepared_task(db, agent_key="unbound-report", bind_tool=False)

    assert await _run_worker(db, FakeModel()) is True

    task = await fetch_task(db, task_id)
    assert task["status"] == "failed"
    assert task["error_code"] == "TOOL_NOT_BOUND"
    assert await fetch_evidences(db, task_id) == []
    audits = await fetch_audits(db, action="tool.invocation")
    assert len(audits) == 1
    assert audits[0]["decision"] == "denied"
    assert audits[0]["reason_code"] == "TOOL_NOT_BOUND"
    assert audits[0]["metadata"]["tool_key"] == "metrics.query"
    events = await fetch_events(db, task_id)
    assert events[-1]["type"] == "task.failed"
    assert "task.completed" not in [event["type"] for event in events]


async def test_invalid_tool_arguments_fail_closed_and_are_audited(migrated_db):
    db = migrated_db
    _, task_id = await _prepared_task(db)
    invalid_plan = {
        "metric": "gmv",
        "metric_version": 1,
        "dimensions": ["region_code"],
        "filters": {},
        "time_range": dict(FROZEN_DATE_RANGE),
        "comparison": "previous_period",
        "limit": 100,
    }
    model = FakeModel(
        scripts={"plan_metrics:0": [FakeScript(content=invalid_plan)]}
    )

    assert await _run_worker(db, model) is True

    task = await fetch_task(db, task_id)
    assert task["status"] == "failed"
    assert task["error_code"] == "TOOL_INPUT_INVALID"
    assert await fetch_evidences(db, task_id) == []
    audits = await fetch_audits(db, action="tool.invocation")
    assert audits[-1]["decision"] == "denied"
    assert audits[-1]["reason_code"] == "TOOL_INPUT_INVALID"
    steps = {step["step_key"]: step for step in await fetch_steps(db, task_id)}
    assert (steps["execute_metrics:0"]["step_key"], steps["execute_metrics:0"]["status"]) == (
        "execute_metrics:0",
        "failed",
    )


async def test_live_region_revocation_is_checked_even_with_authorization_snapshot(
    migrated_db,
):
    db = migrated_db
    _, task_id = await _prepared_task(db)
    conn = await asyncpg.connect(db["role_dsn"]("migrator"))
    try:
        await conn.execute(
            "UPDATE identity.region_grants SET revoked_at = now() "
            "WHERE user_id = '0192a3b4-c5d6-7e8f-9012-3456789abcde'::uuid"
        )
    finally:
        await conn.close()

    assert await _run_worker(db, FakeModel()) is True

    task = await fetch_task(db, task_id)
    assert task["status"] == "failed"
    assert task["error_code"] == "REGION_ACCESS_DENIED"
    audits = await fetch_audits(db, action="tool.invocation")
    assert audits[-1]["decision"] == "denied"
    assert audits[-1]["reason_code"] == "REGION_ACCESS_DENIED"


async def test_missing_evidence_reference_is_rejected_by_validate_report(migrated_db):
    db = migrated_db
    _, task_id = await _prepared_task(db)
    missing_id = "00000000-0000-7000-8000-000000000000"
    compose = {
        "summary": "A conclusion that cites evidence that does not exist.",
        "findings": [
            {"text": "Unsupported finding.", "evidence_ids": [missing_id]}
        ],
        "hypotheses": [{"text": "Possible cause.", "status": "unverified"}],
        "recommendations": ["Verify the source."],
        "limitations": ["Unsupported."],
    }
    model = FakeModel(
        scripts={"compose_analysis:0": [FakeScript(content=compose)]}
    )

    assert await _run_worker(db, model) is True

    task = await fetch_task(db, task_id)
    assert task["status"] == "failed"
    assert task["error_code"] == "REPORT_EVIDENCE_MISSING"
    assert task["result_json"] is None
    steps = {step["step_key"]: step for step in await fetch_steps(db, task_id)}
    assert steps["execute_metrics:0"]["status"] == "confirmed"
    assert steps["compose_analysis:0"]["status"] == "confirmed"
    assert steps["validate_report:0"]["status"] == "failed"
    events = await fetch_events(db, task_id)
    assert "task.completed" not in [event["type"] for event in events]
    assert events[-1]["type"] == "task.failed"


async def test_foreign_task_evidence_reference_is_rejected(migrated_db):
    db = migrated_db
    _, task_id = await _prepared_task(db)
    # A second queued task and a manual evidence row owned by it.
    other_agent = await seed_agent(db, agent_key="other-report")
    other_task_id = await create_task_via_api(db, "other-report")
    conn = await asyncpg.connect(db["role_dsn"]("migrator"))
    try:
        await conn.execute(
            """
            INSERT INTO harness.evidences
                (id, task_id, metric_key, metric_version, unit, current_value,
                 previous_value, absolute_change, change_rate, query_plan_json,
                 result_json, result_set_hash, row_count, data_snapshot_id)
            VALUES ($1::uuid, $2::uuid, 'gmv', 1, 'CNY', 57400.00, 70000.00,
                    -12600.00, -0.18000000, '{}'::jsonb, '{}'::jsonb, $3, 1, $4)
            """,
            "00000000-0000-7000-8000-0000000000aa",
            other_task_id,
            "e" * 64,
            SNAPSHOT_ID,
        )
    finally:
        await conn.close()
    foreign_id = "00000000-0000-7000-8000-0000000000aa"
    compose = {
        "summary": "A conclusion that cites another task's evidence.",
        "findings": [{"text": "Foreign finding.", "evidence_ids": [foreign_id]}],
        "hypotheses": [{"text": "Possible cause.", "status": "unverified"}],
        "recommendations": ["Verify the source."],
        "limitations": ["Foreign evidence."],
    }
    model = FakeModel(
        scripts={"compose_analysis:0": [FakeScript(content=compose)]}
    )

    assert await _run_worker(db, model) is True

    task = await fetch_task(db, task_id)
    assert task["status"] == "failed"
    assert task["error_code"] == "REPORT_EVIDENCE_FOREIGN"
    assert await fetch_task(db, other_task_id) is not None
    assert other_agent["agent_key"] == "other-report"


async def test_worker_uses_frozen_agent_snapshot_not_mutable_profile(migrated_db):
    db = migrated_db
    seeded = await seed_agent(
        db,
        agent_key="snapshot-report",
        published=False,
        profile_timeout_ms=50,
    )
    published = registry_cli(
        db,
        "agent",
        "publish",
        "--version-id",
        seeded["agent_id"],
        "--actor-id",
        str(uuid.uuid4()),
    )
    assert published["model_config_snapshot"]["provider"] == "fake"
    assert published["model_config_snapshot"]["model_name"] == "fake-v1"
    assert published["model_config_snapshot"]["timeout_ms"] == 50
    await complete_dataset_load(db)

    # The profile row is mutable; published Agent runs must ignore these changes.
    registry = await asyncpg.connect(db["role_dsn"]("registry_cli"))
    try:
        await registry.execute(
            "UPDATE registry.model_profiles "
            "SET provider = 'mutated-provider', model_name = 'mutated-model', "
            "    timeout_ms = 5000 WHERE id = $1::uuid",
            seeded["profile_id"],
        )
    finally:
        await registry.close()

    task_id = await create_task_via_api(db, "snapshot-report")
    plan = {
        "metric": "gmv",
        "metric_version": 1,
        "dimensions": ["region_code"],
        "filters": {"region_code": ["CN_EAST"]},
        "time_range": dict(FROZEN_DATE_RANGE),
        "comparison": "previous_period",
        "limit": 100,
    }
    model = FakeModel(
        scripts={
            "plan_metrics:0": [
                FakeScript(content=plan, delay_seconds=0.2)
            ]
        }
    )

    assert await _run_worker(db, model) is True

    # Frozen 50ms timeout wins over the mutated 5000ms profile timeout.
    task = await fetch_task(db, task_id)
    assert task["status"] == "failed"
    assert task["error_code"] == "MODEL_TIMEOUT"
    conn = await asyncpg.connect(db["role_dsn"]("migrator"))
    try:
        invocations = await conn.fetch(
            "SELECT provider, model_name FROM harness.model_invocations "
            "WHERE task_id = $1::uuid ORDER BY started_at, invocation_key",
            task_id,
        )
        profile = await conn.fetchrow(
            "SELECT provider, model_name, timeout_ms FROM registry.model_profiles "
            "WHERE id = $1::uuid",
            seeded["profile_id"],
        )
    finally:
        await conn.close()
    assert dict(profile) == {
        "provider": "mutated-provider",
        "model_name": "mutated-model",
        "timeout_ms": 5000,
    }
    assert [dict(row) for row in invocations] == [
        {"provider": "fake", "model_name": "fake-v1"}
    ]


async def test_result_json_is_persisted_and_events_stay_contiguous(migrated_db):
    db = migrated_db
    _, task_id = await _prepared_task(db)
    assert await _run_worker(db, FakeModel()) is True
    task = await fetch_task(db, task_id)
    events = await fetch_events(db, task_id)
    assert task["result_json"] is not None
    assert json.loads(task["result_json"])["report_version"] == "1.0"
    assert [event["seq"] for event in events] == list(range(1, len(events) + 1))
