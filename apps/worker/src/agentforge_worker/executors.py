"""Registered read-only tool executors. Ticket 07 replaces the metrics stub."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from agentforge_harness.gateway import ToolContext

FROZEN_WINDOW = {
    "start": "2025-03-31",
    "end_exclusive": "2025-04-07",
    "timezone": "UTC",
}
FROZEN_VALUES = {
    "current_value": "57400.00",
    "previous_value": "70000.00",
    "absolute_change": "-12600.00",
    "change_rate": "-0.1800",
}


class MetricsQueryExecutor:
    """Deterministic P0 metrics stub over the frozen fixture window.

    It deliberately reads no business tables: ticket 07 supplies the real
    semantic compiler and read-only SQL executor under this same tool contract.
    """

    tool_key = "metrics.query"
    read_only = True

    async def execute(
        self, request: dict[str, Any], context: ToolContext
    ) -> dict[str, Any]:
        time_range = request.get("time_range") or {}
        regions = set((request.get("filters") or {}).get("region_code") or [])
        frozen = (
            request.get("metric") == "gmv"
            and request.get("metric_version") == 1
            and time_range.get("start") == FROZEN_WINDOW["start"]
            and time_range.get("end_exclusive") == FROZEN_WINDOW["end_exclusive"]
            and time_range.get("timezone") == FROZEN_WINDOW["timezone"]
            and "CN_EAST" in regions
        )
        if frozen:
            values = dict(FROZEN_VALUES)
            row_count = 1400
        else:
            values = {
                "current_value": "0.00",
                "previous_value": "0.00",
                "absolute_change": "0.00",
                "change_rate": None,
            }
            row_count = 0
        return {
            "metric": "gmv",
            "metric_version": 1,
            "unit": "CNY",
            "current_value": values["current_value"],
            "previous_value": values["previous_value"],
            "absolute_change": values["absolute_change"],
            "change_rate": values["change_rate"],
            "row_count": row_count,
            "data_snapshot_id": context.task_snapshot_id,
        }


def decimal_money(value: Any) -> Decimal:
    return Decimal(str(value))
