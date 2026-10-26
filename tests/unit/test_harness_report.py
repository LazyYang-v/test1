"""Pure report-schema and evidence-binding validation (ADR-009)."""

from decimal import Decimal

import pytest

from agentforge_harness.report import (
    REPORT_VERSION,
    Evidence,
    ReportValidationError,
    build_report,
    validate_report,
)

TASK_ID = "0192a3b4-c5d6-7e8f-9012-3456789ab801"
OTHER_TASK_ID = "0192a3b4-c5d6-7e8f-9012-3456789ab8ff"
SNAPSHOT_ID = "e38d3afb175b4d130db4e1600cef8710b448bd67151770f1ca6ad6734e0ab5b8"
OTHER_SNAPSHOT_ID = "f" * 64
EVIDENCE_ID = "0192a3b4-c5d6-7e8f-9012-3456789ab804"
OTHER_EVIDENCE_ID = "0192a3b4-c5d6-7e8f-9012-3456789ab8fe"


def _evidence(**overrides) -> Evidence:
    values = {
        "evidence_id": EVIDENCE_ID,
        "task_id": TASK_ID,
        "data_snapshot_id": SNAPSHOT_ID,
        "metric_key": "gmv",
        "metric_version": 1,
        "unit": "CNY",
        "current_value": Decimal("57400.00"),
        "previous_value": Decimal("70000.00"),
        "absolute_change": Decimal("-12600.00"),
        "change_rate": Decimal("-0.1800"),
        "row_count": 1400,
    }
    values.update(overrides)
    return Evidence(**values)


def _compose(**overrides) -> dict:
    content = {
        "summary": "CN_EAST GMV fell 18.00% versus the previous period.",
        "findings": [
            {
                "text": "Paid GMV decreased by 12600 CNY in the current window.",
                "evidence_ids": [EVIDENCE_ID],
            }
        ],
        "hypotheses": [
            {"text": "A marketing pause may explain the decline.", "status": "unverified"}
        ],
        "recommendations": ["Review campaign coverage for the window."],
        "limitations": ["This analysis covers only the authorized region scope."],
    }
    content.update(overrides)
    return content


def _valid_report() -> dict:
    return build_report(_compose(), [_evidence()])


def test_build_report_adds_platform_limitations_from_evidence_annotations():
    zero_baseline = _evidence(annotations=("ZERO_BASELINE",))
    report = build_report(_compose(), [zero_baseline])
    assert any(
        limitation.startswith("ZERO_BASELINE:")
        for limitation in report["limitations"]
    )
    validate_report(
        report,
        task_id=TASK_ID,
        data_snapshot_id=SNAPSHOT_ID,
        evidences=[zero_baseline],
    )


def test_build_report_annotates_data_insufficiency_once():
    evidence = _evidence(
        current_value=Decimal("0.00"),
        absolute_change=Decimal("-70000.00"),
        change_rate=Decimal("-1.0000"),
        row_count=0,
        annotations=("DATA_INSUFFICIENT",),
    )
    report = build_report(_compose(), [evidence, evidence])
    platform = [
        limitation
        for limitation in report["limitations"]
        if limitation.startswith("DATA_INSUFFICIENT:")
    ]
    assert len(platform) == 1


def test_build_report_uses_platform_values_and_version():
    report = _valid_report()
    assert report["report_version"] == REPORT_VERSION
    assert report["evidence_ids"] == [EVIDENCE_ID]
    assert report["metrics"] == [
        {
            "metric": "gmv",
            "metric_version": 1,
            "unit": "CNY",
            "current_value": "57400.00",
            "previous_value": "70000.00",
            "absolute_change": "-12600.00",
            "change_rate": "-0.1800",
            "evidence_id": EVIDENCE_ID,
            "data_snapshot_id": SNAPSHOT_ID,
        }
    ]
    assert validate_report(
        report,
        task_id=TASK_ID,
        data_snapshot_id=SNAPSHOT_ID,
        evidences=[_evidence()],
    )["summary"].startswith("CN_EAST")


def test_zero_baseline_allows_null_change_rate():
    evidence = _evidence(
        previous_value=Decimal("0.00"),
        current_value=Decimal("100.00"),
        absolute_change=Decimal("100.00"),
        change_rate=None,
    )
    report = build_report(_compose(), [evidence])
    assert report["metrics"][0]["change_rate"] is None
    assert validate_report(
        report, task_id=TASK_ID, data_snapshot_id=SNAPSHOT_ID, evidences=[evidence]
    )


@pytest.mark.parametrize("field", ["summary", "metrics", "findings", "hypotheses",
                                   "recommendations", "limitations", "evidence_ids",
                                   "report_version"])
def test_missing_required_field_is_rejected(field):
    report = _valid_report()
    report.pop(field)
    with pytest.raises(ReportValidationError, match="REPORT_SCHEMA_INVALID"):
        validate_report(
            report, task_id=TASK_ID, data_snapshot_id=SNAPSHOT_ID,
            evidences=[_evidence()],
        )


def test_finding_must_reference_at_least_one_evidence():
    report = _valid_report()
    report["findings"][0]["evidence_ids"] = []
    with pytest.raises(ReportValidationError, match="REPORT_EVIDENCE_MISSING"):
        validate_report(
            report, task_id=TASK_ID, data_snapshot_id=SNAPSHOT_ID,
            evidences=[_evidence()],
        )


def test_finding_cannot_reference_unknown_evidence():
    report = _valid_report()
    report["findings"][0]["evidence_ids"] = [OTHER_EVIDENCE_ID]
    with pytest.raises(ReportValidationError, match="REPORT_EVIDENCE_MISSING"):
        validate_report(
            report, task_id=TASK_ID, data_snapshot_id=SNAPSHOT_ID,
            evidences=[_evidence()],
        )


def test_finding_cannot_reference_evidence_owned_by_another_task():
    foreign = _evidence(evidence_id=OTHER_EVIDENCE_ID, task_id=OTHER_TASK_ID)
    compose = _compose(
        findings=[{"text": "foreign", "evidence_ids": [OTHER_EVIDENCE_ID]}]
    )
    report = build_report(compose, [foreign])
    with pytest.raises(ReportValidationError, match="REPORT_EVIDENCE_FOREIGN"):
        validate_report(
            report, task_id=TASK_ID, data_snapshot_id=SNAPSHOT_ID,
            evidences=[foreign],
        )


def test_finding_cannot_reference_evidence_from_another_snapshot():
    foreign = _evidence(
        evidence_id=OTHER_EVIDENCE_ID, data_snapshot_id=OTHER_SNAPSHOT_ID
    )
    compose = _compose(
        findings=[{"text": "foreign", "evidence_ids": [OTHER_EVIDENCE_ID]}]
    )
    report = build_report(compose, [foreign])
    with pytest.raises(ReportValidationError, match="REPORT_EVIDENCE_SNAPSHOT_MISMATCH"):
        validate_report(
            report, task_id=TASK_ID, data_snapshot_id=SNAPSHOT_ID,
            evidences=[foreign],
        )


def test_metric_values_must_match_platform_evidence_exactly():
    report = _valid_report()
    report["metrics"][0]["current_value"] = "56000.00"
    with pytest.raises(ReportValidationError, match="REPORT_METRIC_VALUE_MISMATCH"):
        validate_report(
            report, task_id=TASK_ID, data_snapshot_id=SNAPSHOT_ID,
            evidences=[_evidence()],
        )


def test_metric_evidence_id_must_exist():
    report = _valid_report()
    report["metrics"][0]["evidence_id"] = OTHER_EVIDENCE_ID
    with pytest.raises(ReportValidationError, match="REPORT_EVIDENCE_MISSING"):
        validate_report(
            report, task_id=TASK_ID, data_snapshot_id=SNAPSHOT_ID,
            evidences=[_evidence()],
        )


def test_hypotheses_must_be_explicitly_unverified():
    report = _valid_report()
    report["hypotheses"][0]["status"] = "verified"
    with pytest.raises(ReportValidationError, match="REPORT_HYPOTHESIS_NOT_UNVERIFIED"):
        validate_report(
            report, task_id=TASK_ID, data_snapshot_id=SNAPSHOT_ID,
            evidences=[_evidence()],
        )


def test_evidence_ids_must_cover_all_references():
    report = _valid_report()
    report["evidence_ids"] = []
    with pytest.raises(ReportValidationError, match="REPORT_EVIDENCE_IDS_INCOMPLETE"):
        validate_report(
            report, task_id=TASK_ID, data_snapshot_id=SNAPSHOT_ID,
            evidences=[_evidence()],
        )


def test_malformed_compose_output_is_rejected_by_build_report():
    compose = _compose(findings=[{"text": "no reference"}])
    with pytest.raises(ReportValidationError):
        build_report(compose, [_evidence()])
