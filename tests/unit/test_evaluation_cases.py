"""Deterministic fixed benchmark assertions against runtime report payloads."""

import pytest
from pydantic import ValidationError

from agentforge_api.config import Settings
from agentforge_api.evaluations import CreateEvaluationRun
from agentforge_core.evaluation import CORE_CASES, SNAPSHOT_ID, evaluate_case


def _report(case, *, current_value, previous_value, absolute_change, change_rate, evidence_id):
    return {
        "metrics": [
            {
                "metric": "gmv",
                "metric_version": 1,
                "unit": "CNY",
                "current_value": current_value,
                "previous_value": previous_value,
                "absolute_change": absolute_change,
                "change_rate": change_rate,
                "evidence_id": evidence_id,
                "data_snapshot_id": SNAPSHOT_ID,
            }
        ],
        "limitations": [],
        "hypotheses": [{"text": "A possible cause", "status": "unverified"}],
        "findings": [{"text": "Verified GMV change", "evidence_ids": [evidence_id]}],
        "evidence_ids": [evidence_id],
    }


def _evaluate(case, *, report=None, actual=None, evidence_plan=None, snapshot=SNAPSHOT_ID):
    task_id = f"task-{case['case_key']}"
    evidence_id = f"evidence-{case['case_key']}"
    metric = actual or case["expected"]
    report = report or _report(
        case,
        current_value=metric.get("current_value", "57400.00"),
        previous_value=metric.get("previous_value", "70000.00"),
        absolute_change=metric.get("absolute_change", "-12600.00"),
        change_rate=metric.get("change_rate"),
        evidence_id=evidence_id,
    )
    evidence = {
        "evidence_id": evidence_id,
        "task_id": task_id,
        "data_snapshot_id": snapshot,
        "query_plan_json": evidence_plan
        or {
            "regions": case["input"]["region_codes"],
            "source_view": "business.gmv_v1",
            "current_period": {
                "start": "2025-01-31T00:00:00+08:00"
                if case["case_key"] == "BA-05"
                else "2025-03-31T00:00:00+00:00"
            },
        },
    }
    return evaluate_case(
        case,
        task_id=task_id,
        task_status="completed",
        task_result=report,
        task_error_code=None,
        task_snapshot_id=snapshot,
        evidences=[evidence],
    )


def test_live_benchmark_requires_explicit_enablement_and_a_bounded_call_budget():
    with pytest.raises(ValidationError):
        CreateEvaluationRun(dataset_key="fixed", agent_key="sales", model_mode="live")

    body = CreateEvaluationRun(
        dataset_key="fixed", agent_key="sales", model_mode="live", live_call_budget=2
    )
    assert body.live_call_budget == 2
    assert Settings().live_evaluations_enabled is False
    assert Settings().live_evaluation_call_budget_limit == 0


def test_fixed_benchmark_has_ten_cases_and_independent_fixture_expectations():
    assert [case["case_key"] for case in CORE_CASES] == [
        f"BA-{index:02d}" for index in range(1, 11)
    ]
    assert all(case["data_snapshot_ref"] == SNAPSHOT_ID for case in CORE_CASES)
    assert CORE_CASES[1]["expected"]["change_rate"] == "-0.1800"
    assert CORE_CASES[2]["assertions"]["required_annotations"] == ["ZERO_BASELINE"]
    assert CORE_CASES[3]["expected"]["current_value"] == "0.00"
    assert CORE_CASES[3]["assertions"]["expected_row_count"] == 0
    assert CORE_CASES[4]["input"]["date_range"]["timezone"] == "Asia/Shanghai"


def test_benchmark_passes_valid_platform_metrics_and_same_task_evidence():
    case = CORE_CASES[1]
    result = _evaluate(
        case,
        actual={
            "current_value": "57400.00",
            "previous_value": "70000.00",
            "absolute_change": "-12600.00",
            "change_rate": "-0.1800",
        },
    )

    assert result["status"] == "passed"
    assert result["failure_codes"] == []
    assert result["score_json"]["checks"]["change_rate"] is True


def test_benchmark_checks_all_ten_numeric_and_structural_cases():
    results = []
    for case in CORE_CASES:
        expected = case["expected"]
        report = _report(
            case,
            current_value=expected.get("current_value", "57400.00"),
            previous_value=expected.get("previous_value", "70000.00"),
            absolute_change=expected.get("absolute_change", "-12600.00"),
            change_rate=expected.get("change_rate"),
            evidence_id=f"evidence-{case['case_key']}",
        )
        evidence_id = f"evidence-{case['case_key']}"
        report["metrics"][0].update(
            {field: value for field, value in expected.items() if field in report["metrics"][0]}
        )
        if "row_count" in expected:
            report["metrics"][0]["row_count"] = expected["row_count"]
        if "expected_row_count" in case["assertions"]:
            report["metrics"][0]["row_count"] = case["assertions"]["expected_row_count"]
        if case["case_key"] == "BA-03":
            report["limitations"] = ["ZERO_BASELINE"]
        if case["case_key"] == "BA-04":
            report["limitations"] = ["DATA_INSUFFICIENT"]
        report["hypotheses"] = [{"text": "Cause to verify", "status": "unverified"}]
        report["findings"] = [{"text": "Metric-backed finding", "evidence_ids": [evidence_id]}]
        results.append(_evaluate(case, report=report))

    assert [result["status"] for result in results] == ["passed"] * 10


def test_benchmark_rejects_verified_hypotheses_and_foreign_evidence():
    case = CORE_CASES[9]
    report = _report(
        case,
        current_value="57400.00",
        previous_value="70000.00",
        absolute_change="-12600.00",
        change_rate="-0.1800",
        evidence_id="evidence-from-another-task",
    )
    report["hypotheses"] = [{"text": "Cause stated as fact", "status": "verified"}]

    result = _evaluate(case, report=report)

    assert result["status"] == "failed"
    assert "EVIDENCE_REFERENCE_ASSERTION_FAILED" in result["failure_codes"]


def test_benchmark_rejects_a_case_with_a_different_data_snapshot():
    result = _evaluate(CORE_CASES[1], snapshot="0" * 64)

    assert result["status"] == "failed"
    assert result["failure_codes"] == ["DATA_SNAPSHOT_MISMATCH"]


def test_timezone_case_requires_the_compiled_local_period():
    case = CORE_CASES[4]
    result = _evaluate(
        case,
        actual={
            "current_value": "73250.53",
            "previous_value": "77316.52",
            "absolute_change": "-4065.99",
            "change_rate": "-0.0526",
        },
        evidence_plan={
            "regions": ["CN_EAST"],
            "current_period": {"start": "2025-01-30T16:00:00+00:00"},
        },
    )

    assert result["status"] == "failed"
    assert result["score_json"]["checks"]["timezone"] is False
