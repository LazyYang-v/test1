"""Deterministic report construction and evidence/numeric validation (BA-10).

The model composes prose only. Metrics, evidence IDs, metric versions, units,
and numeric values come from platform-created Evidence records. The validator
rejects absent, foreign, or snapshot-mismatched evidence and any numeric drift.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Sequence

REPORT_VERSION = "1.0"

_REPORT_KEYS = {
    "summary",
    "metrics",
    "findings",
    "hypotheses",
    "recommendations",
    "limitations",
    "evidence_ids",
    "report_version",
}
_METRIC_KEYS = {
    "metric",
    "metric_version",
    "unit",
    "current_value",
    "previous_value",
    "absolute_change",
    "change_rate",
    "evidence_id",
    "data_snapshot_id",
}


class ReportValidationError(ValueError):
    """Raised when a report is malformed or cannot be traced to evidence."""

    def __init__(self, code: str, detail: str = "", *, evidence_id: str | None = None):
        self.code = code
        self.evidence_id = evidence_id
        super().__init__(f"{code}: {detail}" if detail else code)


@dataclass(frozen=True)
class Evidence:
    evidence_id: str
    task_id: str
    data_snapshot_id: str
    metric_key: str
    metric_version: int
    unit: str
    current_value: Decimal
    previous_value: Decimal
    absolute_change: Decimal
    change_rate: Decimal | None
    row_count: int = 1


def _money(value: Decimal) -> str:
    return f"{Decimal(value):.2f}"


def _rate(value: Decimal | None) -> str | None:
    return None if value is None else f"{Decimal(value):.4f}"


def _metric_from_evidence(evidence: Evidence) -> dict[str, Any]:
    return {
        "metric": evidence.metric_key,
        "metric_version": evidence.metric_version,
        "unit": evidence.unit,
        "current_value": _money(evidence.current_value),
        "previous_value": _money(evidence.previous_value),
        "absolute_change": _money(evidence.absolute_change),
        "change_rate": _rate(evidence.change_rate),
        "evidence_id": evidence.evidence_id,
        "data_snapshot_id": evidence.data_snapshot_id.strip(),
    }


def _check_compose_output(compose: Any) -> dict[str, Any]:
    if not isinstance(compose, dict):
        raise ReportValidationError("REPORT_SCHEMA_INVALID", "compose output is not an object")
    if not isinstance(compose.get("summary"), str) or not compose["summary"].strip():
        raise ReportValidationError("REPORT_SCHEMA_INVALID", "summary is required")
    if not isinstance(compose.get("findings"), list) or not compose["findings"]:
        raise ReportValidationError("REPORT_SCHEMA_INVALID", "findings are required")
    for finding in compose["findings"]:
        if (
            not isinstance(finding, dict)
            or not isinstance(finding.get("text"), str)
            or not finding["text"].strip()
            or not isinstance(finding.get("evidence_ids"), list)
            or not finding["evidence_ids"]
            or not all(isinstance(item, str) for item in finding["evidence_ids"])
        ):
            raise ReportValidationError(
                "REPORT_EVIDENCE_MISSING",
                "each finding must reference at least one evidence id",
            )
    if not isinstance(compose.get("hypotheses"), list):
        raise ReportValidationError("REPORT_SCHEMA_INVALID", "hypotheses are required")
    for hypothesis in compose["hypotheses"]:
        if (
            not isinstance(hypothesis, dict)
            or not isinstance(hypothesis.get("text"), str)
            or hypothesis.get("status") != "unverified"
        ):
            raise ReportValidationError(
                "REPORT_HYPOTHESIS_NOT_UNVERIFIED",
                "every hypothesis must be explicitly unverified",
            )
    for field in ("recommendations", "limitations"):
        value = compose.get(field)
        if not isinstance(value, list) or not all(
            isinstance(item, str) and item.strip() for item in value
        ):
            raise ReportValidationError("REPORT_SCHEMA_INVALID", f"{field} are required")
    return compose


def build_report(
    compose_output: dict[str, Any],
    evidences: Sequence[Evidence],
    *,
    report_version: str = REPORT_VERSION,
) -> dict[str, Any]:
    """Combine model prose with platform-generated metrics and evidence IDs."""
    compose = _check_compose_output(compose_output)
    metrics = [_metric_from_evidence(evidence) for evidence in evidences]
    referenced = {metric["evidence_id"] for metric in metrics}
    for finding in compose["findings"]:
        referenced.update(finding["evidence_ids"])
    return {
        "summary": compose["summary"],
        "metrics": metrics,
        "findings": compose["findings"],
        "hypotheses": compose["hypotheses"],
        "recommendations": compose["recommendations"],
        "limitations": compose["limitations"],
        "evidence_ids": sorted(referenced),
        "report_version": report_version,
    }


def _index_evidence(
    evidences: Sequence[Evidence],
) -> dict[str, Evidence]:
    return {evidence.evidence_id: evidence for evidence in evidences}


def _resolve_evidence(
    index: dict[str, Evidence],
    evidence_id: str,
    *,
    task_id: str,
    data_snapshot_id: str,
) -> Evidence:
    evidence = index.get(evidence_id)
    if evidence is None:
        raise ReportValidationError(
            "REPORT_EVIDENCE_MISSING",
            f"unknown evidence id {evidence_id}",
            evidence_id=evidence_id,
        )
    if evidence.task_id != task_id:
        raise ReportValidationError(
            "REPORT_EVIDENCE_FOREIGN",
            f"evidence {evidence_id} belongs to another task",
        )
    if evidence.data_snapshot_id.strip() != data_snapshot_id.strip():
        raise ReportValidationError(
            "REPORT_EVIDENCE_SNAPSHOT_MISMATCH",
            f"evidence {evidence_id} does not match the task snapshot",
        )
    return evidence


def _as_decimal(value: Any, field: str) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ReportValidationError(
            "REPORT_SCHEMA_INVALID", f"{field} is not a decimal"
        ) from None


def _validate_metric(
    metric: Any,
    index: dict[str, Evidence],
    *,
    task_id: str,
    data_snapshot_id: str,
) -> str:
    if not isinstance(metric, dict) or set(metric) != _METRIC_KEYS:
        raise ReportValidationError(
            "REPORT_SCHEMA_INVALID", "metric row has the wrong shape"
        )
    evidence = _resolve_evidence(
        index,
        str(metric["evidence_id"]),
        task_id=task_id,
        data_snapshot_id=data_snapshot_id,
    )
    if (
        metric["metric"] != evidence.metric_key
        or metric["metric_version"] != evidence.metric_version
        or metric["unit"] != evidence.unit
        or str(metric["data_snapshot_id"]).strip() != evidence.data_snapshot_id.strip()
    ):
        raise ReportValidationError(
            "REPORT_METRIC_VALUE_MISMATCH", "metric identity drifted from evidence"
        )
    expected = _metric_from_evidence(evidence)
    for field in (
        "current_value",
        "previous_value",
        "absolute_change",
        "change_rate",
    ):
        if metric[field] != expected[field]:
            raise ReportValidationError(
                "REPORT_METRIC_VALUE_MISMATCH",
                f"{field} does not match platform evidence",
            )
    # Parse the strings as a second guard against non-numeric output.
    for field in ("current_value", "previous_value", "absolute_change"):
        _as_decimal(metric[field], field)
    if metric["change_rate"] is not None:
        _as_decimal(metric["change_rate"], "change_rate")
    return evidence.evidence_id


def validate_report(
    report: Any,
    *,
    task_id: str,
    data_snapshot_id: str,
    evidences: Sequence[Evidence],
) -> dict[str, Any]:
    """Validate the P0 report schema and every evidence/numeric binding."""
    if not isinstance(report, dict):
        raise ReportValidationError("REPORT_SCHEMA_INVALID", "report is not an object")
    if set(report) != _REPORT_KEYS:
        missing = sorted(_REPORT_KEYS - set(report))
        extra = sorted(set(report) - _REPORT_KEYS)
        raise ReportValidationError(
            "REPORT_SCHEMA_INVALID",
            f"missing={missing}, extra={extra}",
        )
    if not isinstance(report["summary"], str) or not report["summary"].strip():
        raise ReportValidationError("REPORT_SCHEMA_INVALID", "summary is required")
    if report["report_version"] != REPORT_VERSION:
        raise ReportValidationError(
            "REPORT_SCHEMA_INVALID", "report_version is unsupported"
        )

    index = _index_evidence(evidences)
    referenced: set[str] = set()

    if not isinstance(report["metrics"], list):
        raise ReportValidationError("REPORT_SCHEMA_INVALID", "metrics must be a list")
    for metric in report["metrics"]:
        referenced.add(
            _validate_metric(
                metric,
                index,
                task_id=task_id,
                data_snapshot_id=data_snapshot_id,
            )
        )

    if not isinstance(report["findings"], list) or not report["findings"]:
        raise ReportValidationError("REPORT_SCHEMA_INVALID", "findings are required")
    for finding in report["findings"]:
        if (
            not isinstance(finding, dict)
            or set(finding) != {"text", "evidence_ids"}
            or not isinstance(finding["text"], str)
            or not finding["text"].strip()
            or not isinstance(finding["evidence_ids"], list)
        ):
            raise ReportValidationError(
                "REPORT_SCHEMA_INVALID", "finding has the wrong shape"
            )
        if not finding["evidence_ids"]:
            raise ReportValidationError(
                "REPORT_EVIDENCE_MISSING",
                "each finding must reference at least one evidence id",
            )
        for evidence_id in finding["evidence_ids"]:
            if not isinstance(evidence_id, str):
                raise ReportValidationError(
                    "REPORT_SCHEMA_INVALID", "evidence id must be a string"
                )
            _resolve_evidence(
                index,
                evidence_id,
                task_id=task_id,
                data_snapshot_id=data_snapshot_id,
            )
            referenced.add(evidence_id)

    if not isinstance(report["hypotheses"], list):
        raise ReportValidationError("REPORT_SCHEMA_INVALID", "hypotheses must be a list")
    for hypothesis in report["hypotheses"]:
        if (
            not isinstance(hypothesis, dict)
            or set(hypothesis) != {"text", "status"}
            or not isinstance(hypothesis["text"], str)
            or not hypothesis["text"].strip()
        ):
            raise ReportValidationError(
                "REPORT_SCHEMA_INVALID", "hypothesis has the wrong shape"
            )
        if hypothesis["status"] != "unverified":
            raise ReportValidationError(
                "REPORT_HYPOTHESIS_NOT_UNVERIFIED",
                "hypotheses must not be presented as verified facts",
            )

    for field in ("recommendations", "limitations"):
        if not isinstance(report[field], list) or not all(
            isinstance(item, str) and item.strip() for item in report[field]
        ):
            raise ReportValidationError("REPORT_SCHEMA_INVALID", f"{field} are required")

    declared = report["evidence_ids"]
    if not isinstance(declared, list) or not all(isinstance(item, str) for item in declared):
        raise ReportValidationError(
            "REPORT_SCHEMA_INVALID", "evidence_ids must be a list of strings"
        )
    declared_set = set(declared)
    for evidence_id in sorted(declared_set - referenced):
        _resolve_evidence(
            index,
            evidence_id,
            task_id=task_id,
            data_snapshot_id=data_snapshot_id,
        )
    if referenced - declared_set:
        raise ReportValidationError(
            "REPORT_EVIDENCE_IDS_INCOMPLETE",
            "evidence_ids must contain every referenced evidence id",
        )
    if not referenced:
        raise ReportValidationError(
            "REPORT_EVIDENCE_MISSING", "report must reference evidence"
        )
    return report
