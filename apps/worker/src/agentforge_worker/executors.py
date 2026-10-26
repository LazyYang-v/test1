"""Registered read-only tool executor backed by the real frozen GMV view."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

import asyncpg

from agentforge_harness.gateway import ToolContext, ToolError, ToolExecution

from agentforge_worker.metrics import (
    GMV_METRIC,
    GMV_METRIC_VERSION,
    GMV_UNIT,
    QueryPlanError,
    calculate_gmv_delta,
    compile_gmv_query,
)


class MetricsQueryExecutor:
    """Compile a structured plan and execute it as ``agentforge_business_ro``.

    The executor has no model-facing SQL surface. The only SQL text is the
    platform template in :mod:`agentforge_worker.metrics`; every value,
    including the region list and the half-open period instants, is bound as a
    query parameter.
    """

    tool_key = "metrics.query"
    read_only = True

    def __init__(self, dsn: str, *, statement_timeout_ms: int = 3000) -> None:
        if urlsplit(dsn).username != "agentforge_business_ro":
            raise ValueError(
                "metrics executor must connect as agentforge_business_ro"
            )
        self._dsn = dsn
        self._statement_timeout_ms = statement_timeout_ms
        self._pool: asyncpg.Pool | None = None

    async def close(self) -> None:
        pool, self._pool = self._pool, None
        if pool is not None:
            await pool.close()

    async def _get_pool(self) -> asyncpg.Pool:
        if self._pool is None:
            self._pool = await asyncpg.create_pool(
                self._dsn,
                min_size=1,
                max_size=2,
                server_settings={
                    "application_name": "agentforge-worker-metrics",
                    "default_transaction_read_only": "on",
                    "statement_timeout": str(self._statement_timeout_ms),
                    "search_path": "pg_catalog",
                },
            )
        return self._pool

    async def execute(
        self, request: dict[str, Any], context: ToolContext
    ) -> ToolExecution:
        try:
            compiled = compile_gmv_query(
                request,
                region_scope=context.region_scope,
                data_snapshot_id=context.task_snapshot_id,
            )
        except QueryPlanError as error:
            raise ToolError(error.code, str(error)) from error
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction(readonly=True):
                # Belt-and-braces: the pool sets the session default, and every
                # metrics transaction also pins a short local timeout.
                await conn.execute(
                    "SELECT set_config('statement_timeout', $1, true)",
                    str(self._statement_timeout_ms),
                )
                row = await conn.fetchrow(compiled.sql, *compiled.parameters)
        if row is None:  # aggregate query always returns exactly one row
            raise RuntimeError("GMV aggregate query returned no row")

        delta = calculate_gmv_delta(
            current_value=row["current_value"],
            previous_value=row["previous_value"],
            current_row_count=row["current_row_count"],
            previous_row_count=row["previous_row_count"],
        )
        result = {
            "metric": GMV_METRIC,
            "metric_version": GMV_METRIC_VERSION,
            "unit": GMV_UNIT,
            "current_value": f"{delta.current_value:.2f}",
            "previous_value": f"{delta.previous_value:.2f}",
            "absolute_change": f"{delta.absolute_change:.2f}",
            "change_rate": (
                None
                if delta.change_rate is None
                else f"{delta.change_rate:.4f}"
            ),
            "row_count": int(delta.current_row_count),
            "data_snapshot_id": context.task_snapshot_id,
        }
        query_plan = {
            **compiled.query_plan,
            "annotations": list(delta.annotations),
        }
        return ToolExecution(result=result, query_plan=query_plan)
