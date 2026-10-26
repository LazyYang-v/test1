"""Deterministic fixed P0 benchmark cases and their report assertions."""

from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

SNAPSHOT_ID = "e38d3afb175b4d130db4e1600cef8710b448bd67151770f1ca6ad6734e0ab5b8"


def _case(
    key: str,
    start: str,
    end_exclusive: str,
    timezone: str,
    expected: dict[str, Any],
    assertions: dict[str, Any],
    *,
    region_codes: tuple[str, ...] = ("CN_EAST",),
) -> dict[str, Any]:
    return {
        "case_key": key,
        "input": {
            "question": f"Evaluate the frozen fixture for {key}",
            "region_codes": list(region_codes),
            "date_range": {
                "start": start,
                "end_exclusive": end_exclusive,
                "timezone": timezone,
            },
            "compare_mode": "previous_period",
        },
        "expected": expected,
        "assertions": assertions,
        "data_snapshot_ref": SNAPSHOT_ID,
    }


CORE_CASES: tuple[dict[str, Any], ...] = (
    _case(
        "BA-01",
        "2025-01-22",
        "2025-01-29",
        "UTC",
        {"current_value": "509476.21", "previous_value": "56000.00", "absolute_change": "453476.21"},
        {"positive_growth": True},
    ),
    _case(
        "BA-02",
        "2025-03-31",
        "2025-04-07",
        "UTC",
        {"current_value": "57400.00", "previous_value": "70000.00", "absolute_change": "-12600.00", "change_rate": "-0.1800"},
        {"change_rate": "-0.1800"},
    ),
    _case(
        "BA-03",
        "2025-01-15",
        "2025-01-22",
        "UTC",
        {"current_value": "56000.00", "previous_value": "0.00", "absolute_change": "56000.00", "change_rate": None},
        {"required_annotations": ["ZERO_BASELINE"]},
    ),
    _case(
        "BA-04",
        "2025-01-08",
        "2025-01-15",
        "UTC",
        {"current_value": "0.00", "previous_value": "526499.78", "absolute_change": "-526499.78", "change_rate": "-1.0000"},
        {"required_annotations": ["DATA_INSUFFICIENT"], "expected_row_count": 0},
    ),
    _case(
        "BA-05",
        "2025-01-31",
        "2025-02-01",
        "Asia/Shanghai",
        {"current_value": "73250.53", "previous_value": "77316.52", "absolute_change": "-4065.99", "change_rate": "-0.0526"},
        {"timezone": "Asia/Shanghai"},
    ),
    _case(
        "BA-06",
        "2025-01-23",
        "2025-01-24",
        "UTC",
        {"current_value": "73418.81", "previous_value": "72227.60", "absolute_change": "1191.21"},
        {"paid_orders_only": True, "source_view": "business.gmv_v1"},
    ),
    _case(
        "BA-07",
        "2025-02-10",
        "2025-02-11",
        "UTC",
        {"current_value": "71410.41", "previous_value": "76368.69", "absolute_change": "-4958.28"},
        {"refunded_orders_included": True, "source_view": "business.gmv_v1"},
    ),
    _case(
        "BA-08",
        "2025-03-31",
        "2025-04-07",
        "UTC",
        {"current_value": "57400.00", "previous_value": "70000.00", "absolute_change": "-12600.00", "change_rate": "-0.1800"},
        {"authorized_regions_only": ["CN_EAST"]},
    ),
    _case(
        "BA-09",
        "2025-03-31",
        "2025-04-07",
        "UTC",
        {"current_value": "57400.00", "previous_value": "70000.00", "absolute_change": "-12600.00", "change_rate": "-0.1800"},
        {"hypotheses_unverified": True, "source_view": "business.gmv_v1"},
    ),
    _case(
        "BA-10",
        "2025-03-31",
        "2025-04-07",
        "UTC",
        {"current_value": "57400.00"},
        {"findings_reference_valid_evidence": True, "source_view": "business.gmv_v1"},
    ),
)


def evaluate_case(
    case: dict[str, Any],
    *,
    task_id: str,
    task_status: str,
    task_result: Any,
    task_error_code: str | None,
    task_snapshot_id: str,
    evidences: list[dict[str, Any]],
) -> dict[str, Any]:
    """Compare one completed Task's public result and platform Evidence to a case."""
    expected = case["expected"]
    assertions = case["assertions"]
    failures: list[str] = []
    checks: dict[str, bool] = {}
    actual: dict[str, Any] = {}

    if task_snapshot_id.strip() != case["data_snapshot_ref"]:
        failures.append("DATA_SNAPSHOT_MISMATCH")
        return {
            "status": "failed",
            "score_json": {"expected": expected, "actual": actual, "checks": checks},
            "failure_codes": failures,
        }
    if task_status != "completed" or not isinstance(task_result, dict):
        failures.append(task_error_code or "TASK_NOT_COMPLETED")
        return {
            "status": "failed",
            "score_json": {"expected": expected, "actual": actual, "checks": checks},
            "failure_codes": failures,
        }

    metrics = task_result.get("metrics")
    if not isinstance(metrics, list) or len(metrics) != 1 or not isinstance(metrics[0], dict):
        failures.append("REPORT_METRICS_INVALID")
        metrics = []
    metric = metrics[0] if metrics else {}
    for field in (
        "current_value",
        "previous_value",
        "absolute_change",
        "change_rate",
        "row_count",
    ):
        if field in metric:
            actual[field] = metric[field]

    for field, value in expected.items():
        passed = metric.get(field) == value
        checks[field] = passed
        if not passed:
            failures.append("METRIC_ASSERTION_FAILED")

    if "expected_row_count" in assertions:
        passed = metric.get("row_count") == assertions["expected_row_count"]
        checks["row_count"] = passed
        if not passed:
            failures.append("ROW_COUNT_ASSERTION_FAILED")

    if assertions.get("positive_growth"):
        try:
            passed = Decimal(str(metric.get("absolute_change"))) > 0
        except (InvalidOperation, TypeError, ValueError):
            passed = False
        checks["positive_growth"] = passed
        if not passed:
            failures.append("GROWTH_ASSERTION_FAILED")

    if "source_view" in assertions:
        observed_sources = {
            (evidence.get("query_plan_json") or {}).get("source_view")
            for evidence in evidences
        }
        passed = assertions["source_view"] in observed_sources
        checks["source_view"] = passed
        if not passed:
            failures.append("METRIC_SOURCE_ASSERTION_FAILED")

    annotations = {
        annotation
        for evidence in evidences
        for annotation in (evidence.get("query_plan_json") or {}).get("annotations", [])
    }
    for annotation in assertions.get("required_annotations", []):
        passed = annotation in annotations or any(
            annotation in limitation
            for limitation in task_result.get("limitations", [])
            if isinstance(limitation, str)
        )
        checks[annotation] = passed
        if not passed:
            failures.append(annotation)

    if "timezone" in assertions:
        date_range = case["input"]["date_range"]
        zone = ZoneInfo(date_range["timezone"])
        current_start = datetime.combine(
            date.fromisoformat(date_range["start"]), time.min, tzinfo=zone
        )
        passed = (
            date_range["timezone"] == assertions["timezone"]
            and any(
                (evidence.get("query_plan_json") or {}).get("current_period", {}).get("start")
                == current_start.isoformat()
                for evidence in evidences
            )
        )
        checks["timezone"] = passed
        if not passed:
            failures.append("TIMEZONE_ASSERTION_FAILED")

    if assertions.get("paid_orders_only") or assertions.get("refunded_orders_included"):
        passed = (
            metric.get("current_value") == expected["current_value"]
            and all(
                evidence.get("query_plan_json", {}).get("source_view")
                == assertions["source_view"]
                for evidence in evidences
            )
        )
        if assertions.get("paid_orders_only"):
            checks["paid_orders_only"] = passed
        if assertions.get("refunded_orders_included"):
            # The expected total includes the frozen 73.25 refunded-order fixture;
            # requiring the platform GMV view proves the frozen inclusion rule.
            checks["refunded_orders_included"] = passed
        if not passed:
            failures.append("FIXTURE_SEMANTICS_ASSERTION_FAILED")

    if "authorized_regions_only" in assertions:
        observed_regions = {
            region
            for evidence in evidences
            for region in (evidence.get("query_plan_json") or {}).get("regions", [])
        }
        expected_regions = set(assertions["authorized_regions_only"])
        passed = observed_regions == expected_regions
        checks["authorized_regions_only"] = passed
        if not passed:
            failures.append("REGION_SCOPE_ASSERTION_FAILED")

    if assertions.get("hypotheses_unverified"):
        hypotheses = task_result.get("hypotheses")
        passed = isinstance(hypotheses, list) and all(
            isinstance(item, dict) and item.get("status") == "unverified"
            for item in hypotheses
        )
        checks["hypotheses_unverified"] = passed
        if not passed:
            failures.append("HYPOTHESIS_ASSERTION_FAILED")

    if assertions.get("findings_reference_valid_evidence"):
        valid_evidence = {
            evidence["evidence_id"]: evidence
            for evidence in evidences
            if evidence.get("task_id") == task_id
            and str(evidence.get("data_snapshot_id", "")).strip() == case["data_snapshot_ref"]
        }
        findings = task_result.get("findings")
        passed = bool(findings) and all(
            isinstance(finding, dict)
            and isinstance(finding.get("evidence_ids"), list)
            and bool(finding["evidence_ids"])
            and all(evidence_id in valid_evidence for evidence_id in finding["evidence_ids"])
            for finding in findings
        )
        checks["findings_reference_valid_evidence"] = passed
        if not passed:
            failures.append("EVIDENCE_REFERENCE_ASSERTION_FAILED")

    unique_failures = list(dict.fromkeys(failures))
    return {
        "status": "failed" if unique_failures else "passed",
        "score_json": {
            "expected": deepcopy(expected),
            "actual": actual,
            "checks": checks,
        },
        "failure_codes": unique_failures,
    }
