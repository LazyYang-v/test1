"""Fail-closed validation for durable replay and memoization records."""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from typing import Any, Sequence

STEP_SEQUENCE = (
    ("plan_metrics:0", "plan_metrics"),
    ("execute_metrics:0", "execute_metrics"),
    ("compose_analysis:0", "compose_analysis"),
    ("validate_report:0", "validate_report"),
)
REPORT_KEYS = {
    "summary",
    "metrics",
    "findings",
    "hypotheses",
    "recommendations",
    "limitations",
    "evidence_ids",
    "report_version",
}


def _json_value(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def _hash(value: Any) -> str:
    canonical = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _evidence_metric(evidence: dict[str, Any], snapshot_id: str) -> dict[str, Any]:
    return {
        "metric": evidence["metric_key"],
        "metric_version": evidence["metric_version"],
        "unit": evidence["unit"],
        "current_value": f"{Decimal(evidence['current_value']):.2f}",
        "previous_value": f"{Decimal(evidence['previous_value']):.2f}",
        "absolute_change": f"{Decimal(evidence['absolute_change']):.2f}",
        "change_rate": (
            None
            if evidence["change_rate"] is None
            else f"{Decimal(evidence['change_rate']):.4f}"
        ),
        "evidence_id": evidence["evidence_id"],
        "data_snapshot_id": snapshot_id.strip(),
    }


def _report_from_compose(
    compose: dict[str, Any], evidence: dict[str, Any], snapshot_id: str
) -> dict[str, Any]:
    evidence_id = evidence["evidence_id"]
    referenced = {evidence_id}
    for finding in compose["findings"]:
        referenced.update(finding["evidence_ids"])
    limitations = list(compose["limitations"])
    query_plan = _json_value(evidence.get("query_plan_json") or {})
    annotations = query_plan.get("annotations") or []
    if "ZERO_BASELINE" in annotations:
        limitation = (
            "ZERO_BASELINE: the previous comparison period had no paid GMV, so "
            "change_rate is undefined."
        )
        if limitation not in limitations:
            limitations.append(limitation)
    if "DATA_INSUFFICIENT" in annotations:
        limitation = (
            "DATA_INSUFFICIENT: the current period has no paid orders in the "
            "authorized region scope, so the metric is zero and the comparison "
            "may not be meaningful."
        )
        if limitation not in limitations:
            limitations.append(limitation)
    return {
        "summary": compose["summary"],
        "metrics": [_evidence_metric(evidence, snapshot_id)],
        "findings": compose["findings"],
        "hypotheses": compose["hypotheses"],
        "recommendations": compose["recommendations"],
        "limitations": limitations,
        "evidence_ids": sorted(referenced),
        "report_version": "1.0",
    }


def _validate_report(report: Any, evidence_id: str) -> str | None:
    if not isinstance(report, dict) or set(report) != REPORT_KEYS:
        return "report does not match the supported result schema"
    if not isinstance(report["summary"], str) or not report["summary"].strip():
        return "report summary is missing"
    if report["report_version"] != "1.0":
        return "report version is unsupported"
    if not isinstance(report["metrics"], list) or len(report["metrics"]) != 1:
        return "report must contain the single confirmed metric"
    if report["metrics"][0].get("evidence_id") != evidence_id:
        return "report metric does not reference confirmed evidence"
    findings = report["findings"]
    if not isinstance(findings, list) or not findings:
        return "report findings are missing"
    for finding in findings:
        if (
            not isinstance(finding, dict)
            or set(finding) != {"text", "evidence_ids"}
            or not isinstance(finding["text"], str)
            or not finding["text"].strip()
            or evidence_id not in finding["evidence_ids"]
        ):
            return "report finding is not bound to confirmed evidence"
    hypotheses = report["hypotheses"]
    if not isinstance(hypotheses, list) or any(
        not isinstance(item, dict)
        or item.get("status") != "unverified"
        or not isinstance(item.get("text"), str)
        or not item["text"].strip()
        for item in hypotheses
    ):
        return "report contains an invalid hypothesis"
    for field in ("recommendations", "limitations"):
        if not isinstance(report[field], list) or not all(
            isinstance(item, str) and item.strip() for item in report[field]
        ):
            return f"report {field} are missing"
    declared = report["evidence_ids"]
    expected = {evidence_id}
    for finding in findings:
        expected.update(finding["evidence_ids"])
    if not isinstance(declared, list) or set(declared) != expected:
        return "report evidence IDs are inconsistent"
    return None


def validate_recovery_records(
    steps: Sequence[dict[str, Any]],
    tool_invocations: Sequence[dict[str, Any]],
    evidences: Sequence[dict[str, Any]],
    *,
    data_snapshot_id: str,
) -> str | None:
    """Return a diagnostic if durable records cannot safely replay this Task."""
    step_by_key = {step["step_key"]: step for step in steps}
    if len(step_by_key) != len(steps):
        return "recovery contains duplicate step keys"
    confirmed_keys = {
        step["step_key"] for step in steps if step["status"] == "confirmed"
    }
    if len(confirmed_keys) > len(STEP_SEQUENCE):
        return "confirmed step sequence exceeds the supported graph"
    expected_prefix = [STEP_SEQUENCE[index][0] for index in range(len(confirmed_keys))]
    if [key for key, _ in STEP_SEQUENCE if key in confirmed_keys] != expected_prefix:
        return "confirmed step sequence is not a prefix of the supported graph"
    for step in steps:
        if step["status"] != "confirmed":
            continue
        if (step["step_key"], step["graph_node_key"]) not in STEP_SEQUENCE:
            return f"confirmed step {step['step_key']} is unsupported"
        result = _json_value(step["result_json"])
        if not isinstance(result, dict):
            return f"confirmed step {step['step_key']} has no object result"
        if step["result_hash"] != _hash(result):
            return f"confirmed step {step['step_key']} result hash does not match"

    execute = step_by_key.get("execute_metrics:0")
    if execute is None or execute["status"] != "confirmed":
        if {"compose_analysis:0", "validate_report:0"} & confirmed_keys:
            return "confirmed graph state has no durable tool result"
        return None
    execute_result = _json_value(execute["result_json"])
    if not isinstance(execute_result, dict) or set(execute_result) != {
        "tool_result",
        "evidence_id",
    }:
        return "confirmed tool result has an invalid shape"
    evidence_id = execute_result["evidence_id"]
    matches = [evidence for evidence in evidences if evidence["evidence_id"] == evidence_id]
    if len(matches) != 1:
        return "confirmed tool result does not have exactly one evidence record"
    evidence = matches[0]
    if evidence["data_snapshot_id"].strip() != data_snapshot_id.strip():
        return "confirmed tool evidence does not match the task snapshot"

    matching_invocations = [
        invocation
        for invocation in tool_invocations
        if invocation["status"] == "succeeded"
        and invocation.get("evidence_id") == evidence_id
        and _json_value(invocation.get("result_json")) == execute_result["tool_result"]
    ]
    if len(matching_invocations) != 1:
        return "confirmed tool result does not match a successful invocation"
    invocation = matching_invocations[0]
    evidence = {
        **evidence,
        **{
            key: invocation.get(f"evidence_{key}")
            for key in (
                "metric_key",
                "metric_version",
                "unit",
                "current_value",
                "previous_value",
                "absolute_change",
                "change_rate",
                "row_count",
                "query_plan_json",
                "data_snapshot_id",
            )
            if evidence.get(key) is None and invocation.get(f"evidence_{key}") is not None
        },
        "result_json": evidence.get("result_json", invocation.get("evidence_result_json")),
        "result_hash": evidence.get("result_hash", invocation.get("evidence_result_hash")),
        "tool_invocation_id": evidence.get(
            "tool_invocation_id", invocation.get("evidence_invocation_id")
        ),
    }
    if evidence.get("data_snapshot_id") is None:
        evidence["data_snapshot_id"] = invocation.get("evidence_snapshot_id")
    if evidence.get("tool_invocation_id") is not None and invocation.get(
        "invocation_id"
    ) != evidence["tool_invocation_id"]:
        return "confirmed tool result references inconsistent invocation evidence"
    tool_result = execute_result["tool_result"]
    if evidence.get("result_json") is not None and _json_value(
        evidence["result_json"]
    ) != tool_result:
        return "confirmed tool result does not match evidence result"
    expected_hash = _hash(tool_result)
    for record in (evidence, invocation):
        if record.get("result_hash") is not None and record["result_hash"].strip() != expected_hash:
            return "confirmed tool result hash does not match persisted result"
    numeric_precision = {
        "current_value": 2,
        "previous_value": 2,
        "absolute_change": 2,
        "change_rate": 4,
    }
    for evidence_field, result_field in (
        ("metric_key", "metric"),
        ("metric_version", "metric_version"),
        ("unit", "unit"),
        ("current_value", "current_value"),
        ("previous_value", "previous_value"),
        ("absolute_change", "absolute_change"),
        ("change_rate", "change_rate"),
    ):
        evidence_value = evidence.get(evidence_field)
        result_value = tool_result.get(result_field)
        if evidence_field in numeric_precision and evidence_value is not None:
            evidence_number = Decimal(evidence_value)
            if not evidence_number.is_finite():
                return f"confirmed tool evidence {evidence_field} is not finite"
            evidence_value = f"{evidence_number:.{numeric_precision[evidence_field]}f}"
        if evidence_value != result_value:
            if evidence_field == "change_rate" and evidence_value is None:
                if result_value is None:
                    continue
            return f"confirmed tool result does not match evidence {evidence_field}"

    compose = step_by_key.get("compose_analysis:0")
    if compose is not None and compose["status"] == "confirmed":
        expected_report = _report_from_compose(
            _json_value(compose["result_json"]), evidence, data_snapshot_id
        )
        report_diagnostic = _validate_report(expected_report, evidence_id)
        if report_diagnostic is not None:
            return f"confirmed graph state cannot produce a valid report: {report_diagnostic}"

    report_step = step_by_key.get("validate_report:0")
    if report_step is not None and report_step["status"] == "confirmed":
        report = _json_value(report_step["result_json"])
        report_diagnostic = _validate_report(report, evidence_id)
        if report_diagnostic is not None:
            return f"confirmed report is invalid: {report_diagnostic}"
        expected_report = _report_from_compose(
            _json_value(compose["result_json"]), evidence, data_snapshot_id
        )
        if report != expected_report:
            return "confirmed report does not match confirmed graph and evidence state"
    return None
