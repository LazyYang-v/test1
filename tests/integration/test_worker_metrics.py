"""Ticket 07: real read-only GMV SQL, deterministic deltas, and Evidence.

Every scenario runs through the HTTP task seam and a real worker against a
fresh PostgreSQL database loaded from the canonical frozen dataset. Expected
numbers that are not frozen fixtures come from an independent query over
``business.gmv_v1`` as ``agentforge_business_ro``.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from decimal import Decimal

import asyncpg
import pytest

from agentforge_harness.gateway import ToolContext, ToolGateway
from agentforge_harness.models import FakeModel, FakeScript
from agentforge_runtime.store import RuntimeStore
from agentforge_worker.executors import MetricsQueryExecutor
from agentforge_worker.gateway_backend import PostgresToolGatewayBackend
from agentforge_worker.worker import Worker
from agentforge_worker.config import WorkerSettings

from worker_support import (
    FROZEN_DATE_RANGE,
    SNAPSHOT_ID,
    complete_dataset_load,
    create_task_via_api,
    fetch_audits,
    fetch_evidences,
    fetch_task,
    get_task_via_api,
    seed_agent,
)

pytestmark = pytest.mark.integration

ZERO_BASELINE_CURRENT = {
    "start": "2025-01-15",
    "end_exclusive": "2025-01-22",
    "timezone": "UTC",
}
NO_ORDERS_CURRENT = {
    "start": "2025-01-08",
    "end_exclusive": "2025-01-15",
    "timezone": "UTC",
}
CROSS_MONTH_RANGE = {
    "start": "2025-01-31",
    "end_exclusive": "2025-02-01",
    "timezone": "Asia/Shanghai",
}
CROSS_MONTH_INSTANTS = {
    "current_start": "2025-01-30T16:00:00+00:00",
    "current_end": "2025-01-31T16:00:00+00:00",
    "previous_start": "2025-01-29T16:00:00+00:00",
    "previous_end": "2025-01-30T16:00:00+00:00",
}
CROSS_MONTH_PLAN_PERIODS = {
    "current_period": {
        "start": "2025-01-31T00:00:00+08:00",
        "end_exclusive": "2025-02-01T00:00:00+08:00",
    },
    "previous_period": {
        "start": "2025-01-30T00:00:00+08:00",
        "end_exclusive": "2025-01-31T00:00:00+08:00",
    },
}


def _settings(db: dict, **overrides) -> WorkerSettings:
    values = {
        "worker_db_dsn": db["role_dsn"]("worker"),
        "business_ro_db_dsn": db["role_dsn"]("business_ro"),
        "worker_id": "worker-metrics",
        "heartbeat_interval_seconds": 0.05,
        "lease_seconds": 5,
        "poll_interval_seconds": 0.05,
        "model_timeout_seconds": 10,
        "max_model_repairs": 2,
    }
    values.update(overrides)
    return WorkerSettings(**values)


def _plan(
    date_range: dict[str, str],
    *,
    regions: list[str] | None = None,
    dimensions: list[str] | None = None,
) -> dict:
    return {
        "metric": "gmv",
        "metric_version": 1,
        "dimensions": list(dimensions if dimensions is not None else ["region_code"]),
        "filters": {"region_code": list(regions or ["CN_EAST"])},
        "time_range": dict(date_range),
        "comparison": "previous_period",
        "limit": 100,
    }


def _model(plan: dict) -> FakeModel:
    return FakeModel(
        scripts={"plan_metrics:0": [FakeScript(content=plan)]}
    )


async def _run_worker(db: dict, model: FakeModel) -> bool:
    pool = await asyncpg.create_pool(
        db["role_dsn"]("worker"), min_size=1, max_size=5
    )
    store = RuntimeStore(pool, worker_id="worker-metrics", lease_seconds=5)
    worker = Worker(_settings(db), store, model)
    try:
        return await worker.run_once()
    finally:
        await worker.close()


async def _run_plan(db: dict, plan: dict) -> tuple[str, dict]:
    task_id = await create_task_via_api(
        db, region_codes=["CN_EAST"], date_range=plan["time_range"]
    )
    assert await _run_worker(db, _model(plan)) is True
    return task_id, await get_task_via_api(db, task_id)


async def _independent_sums(
    db: dict,
    *,
    current_start: str,
    current_end: str,
    previous_start: str,
    previous_end: str,
    region: str = "CN_EAST",
) -> tuple[Decimal, Decimal, int]:
    def instant(value: str):
        return datetime.fromisoformat(value)

    conn = await asyncpg.connect(db["role_dsn"]("business_ro"))
    try:
        row = await conn.fetchrow(
            """
            SELECT
                COALESCE(SUM(paid_amount) FILTER (
                    WHERE paid_at >= $2 AND paid_at < $3
                ), 0) AS current_value,
                COALESCE(SUM(paid_amount) FILTER (
                    WHERE paid_at >= $4 AND paid_at < $5
                ), 0) AS previous_value,
                COUNT(*) FILTER (
                    WHERE paid_at >= $2 AND paid_at < $3
                ) AS current_rows
            FROM business.gmv_v1
            WHERE data_snapshot_id = $1 AND region_code = $6
            """,
            SNAPSHOT_ID,
            instant(current_start),
            instant(current_end),
            instant(previous_start),
            instant(previous_end),
            region,
        )
    finally:
        await conn.close()
    return row["current_value"], row["previous_value"], row["current_rows"]


def _canonical_hash(value) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
    ).hexdigest()


async def test_real_gmv_evidence_and_report_are_bound_to_the_task_snapshot(
    migrated_db,
):
    db = migrated_db
    await seed_agent(db)
    await complete_dataset_load(db)
    task_id = await create_task_via_api(db)
    plan = _plan(FROZEN_DATE_RANGE)

    assert await _run_worker(db, _model(plan)) is True

    detail = await get_task_via_api(db, task_id)
    assert detail["status"] == "completed"
    report = detail["result"]

    evidences = await fetch_evidences(db, task_id)
    assert len(evidences) == 1
    evidence = evidences[0]
    evidence_id = evidence["evidence_id"]

    assert evidence["metric_key"] == "gmv"
    assert evidence["metric_version"] == 1
    assert evidence["unit"] == "CNY"
    assert evidence["current_value"] == Decimal("57400.00")
    assert evidence["previous_value"] == Decimal("70000.00")
    assert evidence["absolute_change"] == Decimal("-12600.00")
    assert evidence["change_rate"] == Decimal("-0.18000000")
    assert evidence["row_count"] == 1400
    assert evidence["data_snapshot_id"] == SNAPSHOT_ID
    assert evidence["result"]["data_snapshot_id"] == SNAPSHOT_ID
    assert len(evidence["result_set_hash"]) == 64
    assert evidence["result_set_hash"] == _canonical_hash(evidence["result"])

    query_plan = evidence["query_plan"]
    assert query_plan["source_view"] == "business.gmv_v1"
    assert query_plan["aggregation"] == "sum(paid_amount)"
    assert query_plan["regions"] == ["CN_EAST"]
    assert query_plan["data_snapshot_id"] == SNAPSHOT_ID
    assert query_plan["current_period"] == {
        "start": "2025-03-31T00:00:00+00:00",
        "end_exclusive": "2025-04-07T00:00:00+00:00",
    }
    assert query_plan["previous_period"] == {
        "start": "2025-03-24T00:00:00+00:00",
        "end_exclusive": "2025-03-31T00:00:00+00:00",
    }
    assert query_plan["annotations"] == []
    assert "sql" not in query_plan

    # The report is assembled from platform Evidence, not from model numbers.
    assert report["metrics"] == [
        {
            "metric": "gmv",
            "metric_version": 1,
            "unit": "CNY",
            "current_value": "57400.00",
            "previous_value": "70000.00",
            "absolute_change": "-12600.00",
            "change_rate": "-0.1800",
            "evidence_id": evidence_id,
            "data_snapshot_id": SNAPSHOT_ID,
        }
    ]
    assert report["evidence_ids"] == [evidence_id]
    assert report["findings"][0]["evidence_ids"] == [evidence_id]

    task = await fetch_task(db, task_id)
    assert task["data_snapshot_id"].strip() == SNAPSHOT_ID


async def test_zero_baseline_no_orders_and_cross_month_timezone_windows(
    migrated_db,
):
    db = migrated_db
    await seed_agent(db)
    await complete_dataset_load(db)

    # Previous period is the dataset's zero-baseline window.
    zero_task, zero_report = await _run_plan(db, _plan(ZERO_BASELINE_CURRENT))
    zero_evidence = (await fetch_evidences(db, zero_task))[0]
    assert zero_evidence["current_value"] == Decimal("56000.00")
    assert zero_evidence["previous_value"] == Decimal("0.00")
    assert zero_evidence["absolute_change"] == Decimal("56000.00")
    assert zero_evidence["change_rate"] is None
    assert "ZERO_BASELINE" in zero_evidence["query_plan"]["annotations"]
    zero_metrics = zero_report["result"]["metrics"]
    assert zero_metrics[0]["change_rate"] is None
    assert any(
        limitation.startswith("ZERO_BASELINE:")
        for limitation in zero_report["result"]["limitations"]
    )

    # No paid orders in the current period: zero value plus a deterministic
    # data-insufficiency limitation, and no division by the previous value.
    empty_task, empty_report = await _run_plan(db, _plan(NO_ORDERS_CURRENT))
    empty_evidence = (await fetch_evidences(db, empty_task))[0]
    assert empty_evidence["current_value"] == Decimal("0.00")
    assert empty_evidence["row_count"] == 0
    assert empty_evidence["previous_value"] > 0
    assert empty_evidence["change_rate"] == Decimal("-1.00000000")
    assert "DATA_INSUFFICIENT" in empty_evidence["query_plan"]["annotations"]
    assert empty_report["result"]["metrics"][0]["current_value"] == "0.00"
    assert any(
        limitation.startswith("DATA_INSUFFICIENT:")
        for limitation in empty_report["result"]["limitations"]
    )

    # A local-day window in Asia/Shanghai crosses both a month and a UTC date
    # boundary: local 2025-01-31 is 2025-01-30T16:00Z .. 2025-01-31T16:00Z.
    boundary_task, boundary_report = await _run_plan(
        db, _plan(CROSS_MONTH_RANGE)
    )
    boundary_evidence = (await fetch_evidences(db, boundary_task))[0]
    expected_current, expected_previous, expected_rows = await _independent_sums(
        db,
        current_start=CROSS_MONTH_INSTANTS["current_start"],
        current_end=CROSS_MONTH_INSTANTS["current_end"],
        previous_start=CROSS_MONTH_INSTANTS["previous_start"],
        previous_end=CROSS_MONTH_INSTANTS["previous_end"],
    )
    assert boundary_evidence["current_value"] == expected_current
    assert boundary_evidence["previous_value"] == expected_previous
    assert boundary_evidence["row_count"] == expected_rows
    assert boundary_evidence["query_plan"]["current_period"] == (
        CROSS_MONTH_PLAN_PERIODS["current_period"]
    )
    assert boundary_evidence["query_plan"]["previous_period"] == (
        CROSS_MONTH_PLAN_PERIODS["previous_period"]
    )

    # The fixture paid at 2025-01-31 15:30Z is local Jan 31; the fixture paid
    # at 2025-01-31 16:30Z is local Feb 1 and must not leak into the current
    # period.
    business_ro = await asyncpg.connect(db["role_dsn"]("business_ro"))
    try:
        current_fixture = await business_ro.fetchval(
            """
            SELECT SUM(paid_amount) FROM business.gmv_v1
            WHERE data_snapshot_id = $1 AND region_code = 'CN_EAST'
              AND paid_at >= $2 AND paid_at < $3
              AND paid_amount IN (12.34, 23.45)
            """,
            SNAPSHOT_ID,
            datetime.fromisoformat(CROSS_MONTH_INSTANTS["current_start"]),
            datetime.fromisoformat(CROSS_MONTH_INSTANTS["current_end"]),
        )
        previous_fixture = await business_ro.fetchval(
            """
            SELECT SUM(paid_amount) FROM business.gmv_v1
            WHERE data_snapshot_id = $1 AND region_code = 'CN_EAST'
              AND paid_at >= $2 AND paid_at < $3
              AND paid_amount IN (12.34, 23.45)
            """,
            SNAPSHOT_ID,
            datetime.fromisoformat(CROSS_MONTH_INSTANTS["previous_start"]),
            datetime.fromisoformat(CROSS_MONTH_INSTANTS["previous_end"]),
        )
    finally:
        await business_ro.close()
    assert current_fixture == Decimal("12.34")
    assert previous_fixture is None


async def test_mixed_regions_only_return_the_task_scope_and_widening_is_rejected(
    migrated_db,
):
    db = migrated_db
    await seed_agent(db, grants=("CN_EAST",))
    await complete_dataset_load(db)

    # The dataset contains CN_EAST, CN_NORTH and CN_SOUTH rows, but the Task
    # scope is CN_EAST only (BA-08).
    task_id = await create_task_via_api(db, region_codes=["CN_EAST"])
    plan = _plan(FROZEN_DATE_RANGE, regions=["CN_EAST"])
    assert await _run_worker(db, _model(plan)) is True
    detail = await get_task_via_api(db, task_id)
    assert detail["status"] == "completed"
    assert detail["result"]["metrics"][0]["current_value"] == "57400.00"
    evidence = (await fetch_evidences(db, task_id))[0]
    assert evidence["query_plan"]["regions"] == ["CN_EAST"]
    assert evidence["current_value"] == Decimal("57400.00")

    business_ro = await asyncpg.connect(db["role_dsn"]("business_ro"))
    try:
        other_regions = await business_ro.fetchval(
            """
            SELECT COALESCE(SUM(paid_amount), 0) FROM business.gmv_v1
            WHERE data_snapshot_id = $1
              AND region_code IN ('CN_NORTH', 'CN_SOUTH')
              AND paid_at >= TIMESTAMPTZ '2025-03-31 00:00:00+00'
              AND paid_at <  TIMESTAMPTZ '2025-04-07 00:00:00+00'
            """,
            SNAPSHOT_ID,
        )
    finally:
        await business_ro.close()
    assert other_regions > 0

    # Asking for an authorized plus an unauthorized region is a widening
    # attempt and is rejected by the gateway before any SQL runs.
    widening_task = await create_task_via_api(db, region_codes=["CN_EAST"])
    widening_plan = _plan(
        FROZEN_DATE_RANGE, regions=["CN_EAST", "CN_NORTH"]
    )
    assert await _run_worker(db, _model(widening_plan)) is True
    task = await fetch_task(db, widening_task)
    assert task["status"] == "failed"
    assert task["error_code"] == "REGION_ACCESS_DENIED"
    assert await fetch_evidences(db, widening_task) == []
    audits = await fetch_audits(db, action="tool.invocation")
    assert audits[-1]["decision"] == "denied"
    assert audits[-1]["reason_code"] == "REGION_ACCESS_DENIED"

    # Dimensions outside the registered metric definition never reach SQL.
    dimension_task = await create_task_via_api(db, region_codes=["CN_EAST"])
    dimension_plan = _plan(
        FROZEN_DATE_RANGE, regions=["CN_EAST"], dimensions=["customer_id"]
    )
    assert await _run_worker(db, _model(dimension_plan)) is True
    task = await fetch_task(db, dimension_task)
    assert task["status"] == "failed"
    assert task["error_code"] == "QUERY_PLAN_INVALID"
    assert await fetch_evidences(db, dimension_task) == []


class _CountingExecutor:
    tool_key = "metrics.query"
    read_only = True

    def __init__(self, inner: MetricsQueryExecutor) -> None:
        self._inner = inner
        self.calls = 0

    async def execute(self, request, context):
        self.calls += 1
        return await self._inner.execute(request, context)

    async def close(self) -> None:
        await self._inner.close()


async def test_replay_reuses_the_persisted_tool_invocation_without_requerying(
    migrated_db,
):
    db = migrated_db
    await seed_agent(db)
    await complete_dataset_load(db)
    task_id = await create_task_via_api(db)

    pool = await asyncpg.create_pool(
        db["role_dsn"]("worker"), min_size=1, max_size=5
    )
    store = RuntimeStore(pool, worker_id="worker-replay", lease_seconds=30)
    executor = _CountingExecutor(
        MetricsQueryExecutor(db["role_dsn"]("business_ro"))
    )
    gateway = ToolGateway(
        PostgresToolGatewayBackend(pool, store), {"metrics.query": executor}
    )
    try:
        lease = await store.claim()
        assert lease is not None
        task = await store.load_task(task_id)
        context = ToolContext(
            task_id=task["task_id"],
            attempt_id=lease.attempt_id,
            agent_version_id=task["agent_version_id"],
            principal_id=task["owner_user_id"],
            region_scope=tuple(task["region_scope_json"]),
            lease_token=lease.lease_token,
            fencing_epoch=lease.fencing_epoch,
            step_key="execute_metrics:0",
            task_snapshot_id=task["data_snapshot_id"],
        )
        plan = _plan(FROZEN_DATE_RANGE)

        first = await gateway.invoke(context, "metrics.query", plan)
        second = await gateway.invoke(context, "metrics.query", plan)
    finally:
        await executor.close()
        await store.close()

    assert first["reused"] is False
    assert second["reused"] is True
    assert second["result"] == first["result"]
    assert second["evidence_id"] == first["evidence_id"]
    assert executor.calls == 1

    evidences = await fetch_evidences(db, task_id)
    assert len(evidences) == 1
    conn = await asyncpg.connect(db["role_dsn"]("migrator"))
    try:
        invocations = await conn.fetch(
            "SELECT invocation_key, status FROM harness.tool_invocations "
            "WHERE task_id = $1::uuid",
            task_id,
        )
    finally:
        await conn.close()
    assert len(invocations) == 1
    assert invocations[0]["status"] == "succeeded"
    assert invocations[0]["invocation_key"].startswith("execute_metrics:0:")
