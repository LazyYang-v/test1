"""Offline checks for durable replay state and fault-injection vocabulary."""

from __future__ import annotations

import hashlib
import json

import pytest

from agentforge_core.faults import FaultPoint, InjectedCrash, NoopFaultInjector
from agentforge_runtime.recovery import validate_recovery_records


def _hash(value: dict) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        .encode("utf-8")
    ).hexdigest()


def _step(step_key: str, graph_node_key: str, result: dict, **overrides) -> dict:
    return {
        "step_key": step_key,
        "graph_node_key": graph_node_key,
        "status": "confirmed",
        "result_json": result,
        "result_hash": _hash(result),
        "owning_attempt_id": "attempt-1",
        "fencing_epoch": 1,
        **overrides,
    }


def test_confirmed_steps_must_be_a_prefix_of_the_graph():
    steps = [
        _step("plan_metrics:0", "plan_metrics", {"metric": "gmv"}),
        _step("compose_analysis:0", "compose_analysis", {"summary": "stale"}),
    ]

    diagnostic = validate_recovery_records(
        steps, [], [], data_snapshot_id="snapshot-1"
    )

    assert diagnostic is not None
    assert "confirmed step sequence" in diagnostic


def test_confirmed_step_result_hash_must_match_its_persisted_result():
    steps = [
        _step(
            "plan_metrics:0",
            "plan_metrics",
            {"metric": "gmv"},
            result_hash="0" * 64,
        )
    ]

    diagnostic = validate_recovery_records(
        steps, [], [], data_snapshot_id="snapshot-1"
    )

    assert diagnostic is not None
    assert "result hash" in diagnostic


def test_confirmed_tool_step_must_match_its_durable_invocation_and_evidence():
    plan = {"metric": "gmv", "metric_version": 1}
    tool_result = {"metric": "gmv", "current_value": "57400.00"}
    steps = [
        _step("plan_metrics:0", "plan_metrics", plan),
        _step(
            "execute_metrics:0",
            "execute_metrics",
            {"tool_result": tool_result, "evidence_id": "evidence-1"},
        ),
    ]

    diagnostic = validate_recovery_records(
        steps, [], [], data_snapshot_id="snapshot-1"
    )

    assert diagnostic is not None
    assert "tool result" in diagnostic or "evidence" in diagnostic


def test_valid_confirmed_tool_result_matches_invocation_and_snapshot_evidence():
    plan = {"metric": "gmv", "metric_version": 1}
    tool_result = {
        "metric": "gmv",
        "metric_version": 1,
        "unit": "CNY",
        "current_value": "57400.00",
        "previous_value": "70000.00",
        "absolute_change": "-12600.00",
        "change_rate": "-0.1800",
        "row_count": 1400,
        "data_snapshot_id": "snapshot-1",
    }
    invocation_id = "invocation-1"
    evidence_id = "evidence-1"
    steps = [
        _step("plan_metrics:0", "plan_metrics", plan),
        _step(
            "execute_metrics:0",
            "execute_metrics",
            {"tool_result": tool_result, "evidence_id": evidence_id},
        ),
    ]
    persisted_hash = _hash(tool_result)
    invocations = [{
        "invocation_id": invocation_id,
        "status": "succeeded",
        "result_json": tool_result,
        "evidence_id": evidence_id,
        # The store loader returns evidence_* columns for joined evidence.
        "evidence_metric_key": "gmv",
        "evidence_metric_version": 1,
        "evidence_unit": "CNY",
        "evidence_current_value": "57400.00",
        "evidence_previous_value": "70000.00",
        "evidence_absolute_change": "-12600.00",
        "evidence_change_rate": "-0.1800",
        "evidence_row_count": 1400,
        "evidence_snapshot_id": "snapshot-1",
        "evidence_invocation_id": invocation_id,
        "evidence_result_json": tool_result,
        "evidence_result_hash": persisted_hash,
    }]
    evidences = [{
        "evidence_id": evidence_id,
        "tool_invocation_id": invocation_id,
        "data_snapshot_id": "snapshot-1",
        "metric_key": "gmv",
        "metric_version": 1,
        "unit": "CNY",
        "current_value": "57400.00",
        "previous_value": "70000.00",
        "absolute_change": "-12600.00",
        "change_rate": "-0.1800",
        "row_count": 1400,
        "result_json": tool_result,
        "result_hash": persisted_hash,
    }]

    assert validate_recovery_records(
        steps, invocations, evidences, data_snapshot_id="snapshot-1"
    ) is None


def test_valid_tool_invocation_without_confirmed_execution_step_is_replayable():
    invocations = [{
        "invocation_id": "invocation-1",
        "status": "succeeded",
        "result_json": {"metric": "gmv"},
        "evidence_id": "evidence-1",
    }]
    evidences = [{
        "evidence_id": "evidence-1",
        "tool_invocation_id": "invocation-1",
        "data_snapshot_id": "snapshot-1",
    }]

    assert validate_recovery_records(
        [_step("plan_metrics:0", "plan_metrics", {"metric": "gmv"})],
        invocations,
        evidences,
        data_snapshot_id="snapshot-1",
    ) is None


def test_injected_crash_signal_is_not_a_normal_exception():
    assert not isinstance(InjectedCrash("fault point reached"), Exception)


@pytest.mark.asyncio
async def test_noop_fault_injector_accepts_every_named_crash_window():
    injector = NoopFaultInjector()

    for point in FaultPoint:
        await injector.checkpoint(point, {"task_id": "task-1"})

    assert len(tuple(FaultPoint)) >= 8
    assert FaultPoint.TOOL_RESULT_BEFORE_COMMIT in tuple(FaultPoint)
    assert FaultPoint.STEP_CONFIRM_BEFORE_COMMIT in tuple(FaultPoint)
    assert FaultPoint.GRAPH_CHECKPOINT_SAVED in tuple(FaultPoint)
    assert FaultPoint.TASK_CREATE_BEFORE_COMMIT in tuple(FaultPoint)
    assert FaultPoint.TASK_CREATE_AFTER_COMMIT in tuple(FaultPoint)
    assert FaultPoint.MODEL_CALL_IN_FLIGHT in tuple(FaultPoint)
    assert FaultPoint.MODEL_RETURNED in tuple(FaultPoint)
    assert FaultPoint.TERMINAL_BEFORE_COMMIT in tuple(FaultPoint)
    assert FaultPoint.RECOVERY_SCAN_BEFORE_COMMIT in tuple(FaultPoint)
