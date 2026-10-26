"""Worker model summaries include bounded, verified dimension breakdowns."""

from decimal import Decimal

from agentforge_worker.graph import AnalysisGraph


def test_evidence_summary_passes_bounded_dimension_results_to_compose():
    evidence = {
        "evidence_id": "evidence-1",
        "metric_key": "gmv",
        "metric_version": 1,
        "unit": "CNY",
        "current_value": Decimal("300.00"),
        "previous_value": Decimal("150.00"),
        "absolute_change": Decimal("150.00"),
        "change_rate": Decimal("1.0000"),
        "data_snapshot_id": "snapshot-1",
        "query_plan_json": {
            "annotations": [],
            "dimensions": ["region_code"],
            "dimension_results": [
                {
                    "dimensions": {
                        "region_code": "CN_EAST",
                        "extra_dimension": "ignored",
                    },
                    "current_value": "300.00",
                    "previous_value": "150.00",
                    "absolute_change": "150.00",
                    "change_rate": "1.0000",
                    "row_count": 3,
                    "annotations": [],
                }
                for _ in range(101)
            ],
        },
    }

    summary = AnalysisGraph._evidence_summary(evidence)

    assert summary["dimension_results"] == [
        {
            "dimensions": {"region_code": "CN_EAST"},
            "current_value": "300.00",
            "previous_value": "150.00",
            "absolute_change": "150.00",
            "change_rate": "1.0000",
            "row_count": 3,
            "annotations": [],
        }
    ] * 100
