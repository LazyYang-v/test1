"""Registered read-only tool executor backed by the real frozen GMV view."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

import asyncpg

from agentforge_harness.gateway import ToolContext, ToolError, ToolExecution

from agentforge_worker.metrics import (
    MetricDefinition,
    MetricPlanner,
    QueryPlanError,
    ResultNormalizer,
    compile_metric_query,
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
            intent = MetricPlanner.plan(request)
        except QueryPlanError as error:
            raise ToolError(error.code, str(error)) from error

        pool = await self._get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction(readonly=True):
                # Both metadata and business rows use the dedicated least-
                # privilege connection, pinned to a fixed search path/timeout.
                await conn.execute(
                    "SELECT set_config('statement_timeout', $1, true)",
                    str(self._statement_timeout_ms),
                )
                try:
                    definition_row = await conn.fetchrow(
                        """
                        SELECT metric_key, version, source_view, expression_key,
                               allowed_dimensions, allowed_filters, aggregation,
                               unit, time_column, status
                        FROM business.metric_definitions
                        WHERE metric_key = $1 AND version = $2 AND status = 'active'
                        """,
                        intent.metric,
                        intent.metric_version,
                    )
                except asyncpg.InsufficientPrivilegeError as error:
                    raise RuntimeError(
                        "business_ro cannot read registered metric definitions"
                    ) from error
                definition = (
                    None
                    if definition_row is None
                    else MetricDefinition.from_row(definition_row)
                )
                try:
                    compiled = compile_metric_query(
                        request,
                        definition=definition,
                        region_scope=context.region_scope,
                        data_snapshot_id=context.task_snapshot_id,
                    )
                except QueryPlanError as error:
                    raise ToolError(error.code, str(error)) from error
                rows = await conn.fetch(compiled.sql, *compiled.parameters)
        if len(rows) > compiled.query_plan["limit"]:
            raise ToolError(
                "QUERY_RESULT_LIMIT_EXCEEDED",
                "metric query reached the configured row limit",
            )
        normalized = ResultNormalizer.normalize(rows)
        result = {
            "metric": compiled.query_plan["metric"],
            "metric_version": compiled.query_plan["metric_version"],
            "unit": compiled.query_plan["unit"],
            "current_value": f"{normalized.current_value:.2f}",
            "previous_value": f"{normalized.previous_value:.2f}",
            "absolute_change": f"{normalized.absolute_change:.2f}",
            "change_rate": (
                None
                if normalized.change_rate is None
                else f"{normalized.change_rate:.4f}"
            ),
            "row_count": int(normalized.row_count),
            "data_snapshot_id": context.task_snapshot_id,
        }
        query_plan = {
            **compiled.query_plan,
            "dimension_results": list(normalized.dimension_results),
            "annotations": list(normalized.annotations),
        }
        return ToolExecution(result=result, query_plan=query_plan)
