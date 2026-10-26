"""Focused GMV v1 semantic template, compiler, and deterministic delta math.

Ticket 07 deliberately implements only the registered ``gmv`` v1 metric from
migration 0003. Ticket 10 replaces this focused template with the generalized
``metric_definitions``-driven compiler; the security properties here are the
ones that must survive that change:

* the model can choose only a metric version, dimensions, a date range,
  comparison mode, and a row limit from the registered schema;
* the SQL text and the region filter are platform-owned;
* every value is bound as a query parameter;
* the task's authorized region scope can only narrow the plan's filter.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

GMV_METRIC = "gmv"
GMV_METRIC_VERSION = 1
GMV_UNIT = "CNY"
GMV_SOURCE_VIEW = "business.gmv_v1"
GMV_TIME_COLUMN = "paid_at"
GMV_AGGREGATION = "sum(paid_amount)"
GMV_ALLOWED_DIMENSIONS = frozenset({"region_code"})
GMV_ALLOWED_FILTERS = frozenset({"region_code"})
MAX_RANGE_DAYS = 90

ZERO_BASELINE = "ZERO_BASELINE"
DATA_INSUFFICIENT = "DATA_INSUFFICIENT"

_MONEY_QUANTUM = Decimal("0.01")
_RATE_QUANTUM = Decimal("0.0001")

_GMV_SQL = """
SELECT
    COALESCE(SUM(paid_amount) FILTER (
        WHERE paid_at >= $3 AND paid_at < $4
    ), 0::numeric) AS current_value,
    COALESCE(SUM(paid_amount) FILTER (
        WHERE paid_at >= $5 AND paid_at < $6
    ), 0::numeric) AS previous_value,
    COUNT(*) FILTER (
        WHERE paid_at >= $3 AND paid_at < $4
    ) AS current_row_count,
    COUNT(*) FILTER (
        WHERE paid_at >= $5 AND paid_at < $6
    ) AS previous_row_count
FROM business.gmv_v1
WHERE data_snapshot_id = $1
  AND region_code = ANY($2::text[])
  AND paid_at >= $5
  AND paid_at < $4
LIMIT $7
"""


class QueryPlanError(ValueError):
    """A structured plan is outside the registered metric schema."""

    code = "QUERY_PLAN_INVALID"


@dataclass(frozen=True)
class CompiledGmvQuery:
    sql: str
    parameters: tuple[Any, ...]
    query_plan: dict[str, Any]


@dataclass(frozen=True)
class GmvDelta:
    current_value: Decimal
    previous_value: Decimal
    absolute_change: Decimal
    change_rate: Decimal | None
    current_row_count: int
    previous_row_count: int
    annotations: tuple[str, ...]


def _require_iso_date(value: Any, field: str) -> date:
    if not isinstance(value, str):
        raise QueryPlanError(f"{field} must be an ISO date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise QueryPlanError(f"{field} must be an ISO date") from None
    if parsed.isoformat() != value:
        raise QueryPlanError(f"{field} must use YYYY-MM-DD")
    return parsed


def _require_timezone(value: Any) -> ZoneInfo:
    if not isinstance(value, str) or not value:
        raise QueryPlanError("time_range.timezone must name an IANA timezone")
    try:
        return ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        raise QueryPlanError("time_range.timezone must name an IANA timezone") from None


def _require_dimensions(value: Any) -> list[str]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) for item in value
    ):
        raise QueryPlanError("dimensions must be a list of strings")
    if len(value) > 4:
        raise QueryPlanError("dimensions may contain at most four entries")
    unknown = [item for item in value if item not in GMV_ALLOWED_DIMENSIONS]
    if unknown:
        raise QueryPlanError(
            f"dimensions are outside the registered schema: {sorted(unknown)}"
        )
    return list(dict.fromkeys(value))


def _require_regions(value: Any) -> list[str]:
    if not isinstance(value, list) or not value:
        raise QueryPlanError("filters.region_code must contain at least one region")
    if any(not isinstance(item, str) or not item.strip() for item in value):
        raise QueryPlanError("filters.region_code must contain non-empty strings")
    return sorted({item.strip() for item in value})


def _require_limit(value: Any) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < 1
        or value > 100
    ):
        raise QueryPlanError("limit must be an integer between 1 and 100")
    return value


def compile_gmv_query(
    plan: dict[str, Any],
    *,
    region_scope: tuple[str, ...],
    data_snapshot_id: str,
) -> CompiledGmvQuery:
    """Validate a structured GMV plan and compile it to parameterized SQL."""
    if not isinstance(plan, dict):
        raise QueryPlanError("query plan must be an object")
    if plan.get("metric") != GMV_METRIC:
        raise QueryPlanError("metric is not registered")
    if plan.get("metric_version") != GMV_METRIC_VERSION:
        raise QueryPlanError("metric version is not registered")
    if plan.get("comparison") != "previous_period":
        raise QueryPlanError("comparison mode is not supported")

    dimensions = _require_dimensions(plan.get("dimensions"))
    filters = plan.get("filters")
    if not isinstance(filters, dict) or set(filters) != GMV_ALLOWED_FILTERS:
        raise QueryPlanError("filters are outside the registered schema")
    requested_regions = _require_regions(filters.get("region_code"))

    time_range = plan.get("time_range")
    if not isinstance(time_range, dict) or set(time_range) != {
        "start",
        "end_exclusive",
        "timezone",
    }:
        raise QueryPlanError("time_range has the wrong shape")
    start = _require_iso_date(time_range["start"], "time_range.start")
    end = _require_iso_date(time_range["end_exclusive"], "time_range.end_exclusive")
    zone = _require_timezone(time_range["timezone"])
    duration_days = (end - start).days
    if duration_days <= 0 or duration_days > MAX_RANGE_DAYS:
        raise QueryPlanError(
            f"date range must be between 1 and {MAX_RANGE_DAYS} calendar days"
        )
    limit = _require_limit(plan.get("limit"))

    # The model may narrow the authorized scope, never widen it. The gateway
    # rejects any region outside the Task scope before dispatch; this
    # intersection is the second, executor-local guard.
    scope = {region.strip() for region in region_scope if region.strip()}
    effective_regions = [region for region in requested_regions if region in scope]
    if not effective_regions:
        raise QueryPlanError("no authorized region remains after applying the Task scope")

    current_start = datetime.combine(start, time.min, tzinfo=zone)
    current_end = datetime.combine(end, time.min, tzinfo=zone)
    previous_start = datetime.combine(
        start - timedelta(days=duration_days), time.min, tzinfo=zone
    )
    previous_end = current_start

    query_plan = {
        "metric": GMV_METRIC,
        "metric_version": GMV_METRIC_VERSION,
        "unit": GMV_UNIT,
        "source_view": GMV_SOURCE_VIEW,
        "aggregation": GMV_AGGREGATION,
        "time_column": GMV_TIME_COLUMN,
        "dimensions": dimensions,
        "regions": effective_regions,
        "data_snapshot_id": data_snapshot_id,
        "current_period": {
            "start": current_start.isoformat(),
            "end_exclusive": current_end.isoformat(),
        },
        "previous_period": {
            "start": previous_start.isoformat(),
            "end_exclusive": previous_end.isoformat(),
        },
        "comparison": "previous_period",
        "limit": limit,
        "parameters": [
            "data_snapshot_id",
            "region_code",
            "current_start",
            "current_end_exclusive",
            "previous_start",
            "previous_end_exclusive",
            "limit",
        ],
        "annotations": [],
    }
    parameters = (
        data_snapshot_id,
        effective_regions,
        current_start,
        current_end,
        previous_start,
        previous_end,
        limit,
    )
    return CompiledGmvQuery(sql=_GMV_SQL, parameters=parameters, query_plan=query_plan)


def _money(value: Any) -> Decimal:
    return Decimal(value).quantize(_MONEY_QUANTUM, rounding=ROUND_HALF_UP)


def calculate_gmv_delta(
    *,
    current_value: Any,
    previous_value: Any,
    current_row_count: int,
    previous_row_count: int,
) -> GmvDelta:
    """Compute change deterministically with Decimal and no zero division."""
    current = _money(current_value)
    previous = _money(previous_value)
    absolute = _money(current - previous)
    annotations: list[str] = []
    if previous == 0:
        change_rate = None
        annotations.append(ZERO_BASELINE)
    else:
        change_rate = ((current - previous) / previous).quantize(
            _RATE_QUANTUM, rounding=ROUND_HALF_UP
        )
    if current_row_count == 0:
        annotations.append(DATA_INSUFFICIENT)
    return GmvDelta(
        current_value=current,
        previous_value=previous,
        absolute_change=absolute,
        change_rate=change_rate,
        current_row_count=current_row_count,
        previous_row_count=previous_row_count,
        annotations=tuple(annotations),
    )
