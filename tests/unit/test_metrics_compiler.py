"""Focused GMV v1 semantic template and deterministic change arithmetic."""

from decimal import Decimal

import pytest

from agentforge_worker.metrics import (
    DATA_INSUFFICIENT,
    ZERO_BASELINE,
    QueryPlanError,
    calculate_gmv_delta,
    compile_gmv_query,
)

SNAPSHOT_ID = "a" * 64


def _plan(**overrides):
    plan = {
        "metric": "gmv",
        "metric_version": 1,
        "dimensions": ["region_code"],
        "filters": {"region_code": ["CN_EAST"]},
        "time_range": {
            "start": "2025-03-31",
            "end_exclusive": "2025-04-07",
            "timezone": "UTC",
        },
        "comparison": "previous_period",
        "limit": 100,
    }
    plan.update(overrides)
    return plan


def test_compile_builds_half_open_current_and_previous_periods():
    compiled = compile_gmv_query(
        _plan(), region_scope=("CN_EAST",), data_snapshot_id=SNAPSHOT_ID
    )
    plan = compiled.query_plan

    assert plan["source_view"] == "business.gmv_v1"
    assert plan["aggregation"] == "sum(paid_amount)"
    assert plan["regions"] == ["CN_EAST"]
    assert plan["current_period"] == {
        "start": "2025-03-31T00:00:00+00:00",
        "end_exclusive": "2025-04-07T00:00:00+00:00",
    }
    assert plan["previous_period"] == {
        "start": "2025-03-24T00:00:00+00:00",
        "end_exclusive": "2025-03-31T00:00:00+00:00",
    }
    assert plan["data_snapshot_id"] == SNAPSHOT_ID
    assert plan["annotations"] == []
    assert "sql" not in plan
    assert compiled.parameters[0] == SNAPSHOT_ID
    assert compiled.parameters[1] == ["CN_EAST"]


def test_compile_uses_the_plan_timezone_across_a_month_boundary():
    compiled = compile_gmv_query(
        _plan(
            time_range={
                "start": "2025-01-31",
                "end_exclusive": "2025-02-01",
                "timezone": "Asia/Shanghai",
            }
        ),
        region_scope=("CN_EAST",),
        data_snapshot_id=SNAPSHOT_ID,
    )
    plan = compiled.query_plan

    assert plan["current_period"] == {
        "start": "2025-01-31T00:00:00+08:00",
        "end_exclusive": "2025-02-01T00:00:00+08:00",
    }
    assert plan["previous_period"] == {
        "start": "2025-01-30T00:00:00+08:00",
        "end_exclusive": "2025-01-31T00:00:00+08:00",
    }


def test_compile_intersects_with_the_task_scope_and_never_widens():
    compiled = compile_gmv_query(
        _plan(filters={"region_code": ["CN_NORTH", "CN_SOUTH"]}),
        region_scope=("CN_EAST", "CN_NORTH"),
        data_snapshot_id=SNAPSHOT_ID,
    )
    assert compiled.query_plan["regions"] == ["CN_NORTH"]
    assert compiled.parameters[1] == ["CN_NORTH"]


@pytest.mark.parametrize(
    "plan",
    [
        _plan(metric="orders"),
        _plan(metric_version=2),
        _plan(dimensions=["customer_id"]),
        _plan(dimensions="region_code"),
        _plan(comparison="year_over_year"),
        _plan(limit=101),
        _plan(limit=True),
        _plan(
            time_range={
                "start": "2025-03-31",
                "end_exclusive": "2025-03-31",
                "timezone": "UTC",
            }
        ),
        _plan(
            time_range={
                "start": "2025-03-31",
                "end_exclusive": "2025-04-07",
                "timezone": "Mars/Olympus",
            }
        ),
        _plan(
            time_range={
                "start": "2025-3-31",
                "end_exclusive": "2025-04-07",
                "timezone": "UTC",
            }
        ),
        _plan(filters={"region_code": ["CN_SOUTH"]}),
        _plan(filters={"region_code": []}),
    ],
)
def test_compile_rejects_plans_outside_the_registered_gmv_schema(plan):
    with pytest.raises(QueryPlanError):
        compile_gmv_query(
            plan, region_scope=("CN_EAST",), data_snapshot_id=SNAPSHOT_ID
        )


def test_delta_is_deterministic_decimal_with_four_place_rate():
    delta = calculate_gmv_delta(
        current_value=Decimal("57400.00"),
        previous_value=Decimal("70000.00"),
        current_row_count=1400,
        previous_row_count=1400,
    )
    assert delta.absolute_change == Decimal("-12600.00")
    assert delta.change_rate == Decimal("-0.1800")
    assert delta.annotations == ()


def test_delta_rounds_the_rate_half_up_to_four_places():
    delta = calculate_gmv_delta(
        current_value=Decimal("1.00"),
        previous_value=Decimal("3.00"),
        current_row_count=1,
        previous_row_count=1,
    )
    assert delta.change_rate == Decimal("-0.6667")


def test_zero_previous_period_never_divides_and_annotates():
    delta = calculate_gmv_delta(
        current_value=Decimal("56000.00"),
        previous_value=Decimal("0.00"),
        current_row_count=1400,
        previous_row_count=0,
    )
    assert delta.absolute_change == Decimal("56000.00")
    assert delta.change_rate is None
    assert ZERO_BASELINE in delta.annotations


def test_zero_current_period_is_annotated_as_data_insufficient():
    delta = calculate_gmv_delta(
        current_value=Decimal("0.00"),
        previous_value=Decimal("12000.00"),
        current_row_count=0,
        previous_row_count=100,
    )
    assert delta.current_value == Decimal("0.00")
    assert delta.change_rate == Decimal("-1.0000")
    assert DATA_INSUFFICIENT in delta.annotations


def test_both_periods_empty_is_zero_without_division():
    delta = calculate_gmv_delta(
        current_value=Decimal("0.00"),
        previous_value=Decimal("0.00"),
        current_row_count=0,
        previous_row_count=0,
    )
    assert delta.absolute_change == Decimal("0.00")
    assert delta.change_rate is None
    assert set(delta.annotations) == {ZERO_BASELINE, DATA_INSUFFICIENT}
