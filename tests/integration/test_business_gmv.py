"""Frozen GMV and business_ro behavior against real PostgreSQL."""

from __future__ import annotations

from pathlib import Path

import asyncpg
import pytest

from agentforge_core.business_dataset import write_dataset
from agentforge_core.business_loader import load_dataset

pytestmark = pytest.mark.integration


async def test_business_ro_queries_frozen_gmv_and_has_view_only_read_access(
    migrated_db, tmp_path: Path
):
    dataset_path = tmp_path / "business.jsonl"
    snapshot_id = write_dataset(dataset_path)
    migrator = await asyncpg.connect(migrated_db["role_dsn"]("migrator"))
    try:
        await load_dataset(migrator, dataset_path)
    finally:
        await migrator.close()

    business_ro = await asyncpg.connect(migrated_db["role_dsn"]("business_ro"))
    try:
        # Snapshot identity is exposed through the authorized metric view.
        snapshot_ids = await business_ro.fetch(
            "SELECT DISTINCT data_snapshot_id FROM business.gmv_v1"
        )
        assert {row["data_snapshot_id"].strip() for row in snapshot_ids} == {snapshot_id}

        east_totals = await business_ro.fetchrow(
            """
            SELECT
                COALESCE(SUM(paid_amount) FILTER (
                    WHERE paid_at >= TIMESTAMPTZ '2025-01-08 00:00:00+00'
                      AND paid_at <  TIMESTAMPTZ '2025-01-15 00:00:00+00'
                ), 0) AS zero_baseline,
                COALESCE(SUM(paid_amount) FILTER (
                    WHERE paid_at >= TIMESTAMPTZ '2025-01-15 00:00:00+00'
                      AND paid_at <  TIMESTAMPTZ '2025-01-22 00:00:00+00'
                ), 0) AS positive_period,
                COALESCE(SUM(paid_amount) FILTER (
                    WHERE paid_at >= TIMESTAMPTZ '2025-03-24 00:00:00+00'
                      AND paid_at <  TIMESTAMPTZ '2025-03-31 00:00:00+00'
                ), 0) AS decline_baseline,
                COALESCE(SUM(paid_amount) FILTER (
                    WHERE paid_at >= TIMESTAMPTZ '2025-03-31 00:00:00+00'
                      AND paid_at <  TIMESTAMPTZ '2025-04-07 00:00:00+00'
                ), 0) AS decline_current
            FROM business.gmv_v1
            WHERE region_code = 'CN_EAST'
            """
        )
        assert east_totals["zero_baseline"] == 0
        assert east_totals["positive_period"] == 56000
        assert east_totals["decline_baseline"] == 70000
        assert east_totals["decline_current"] == 57400
        assert (
            east_totals["decline_current"] - east_totals["decline_baseline"]
        ) / east_totals["decline_baseline"] == pytest.approx(-0.18)

        local_day = await business_ro.fetchval(
            """
            SELECT SUM(paid_amount)
            FROM business.gmv_v1
            WHERE region_code = 'CN_EAST'
              AND paid_at >= TIMESTAMPTZ '2025-01-31 00:00:00+08'
              AND paid_at < TIMESTAMPTZ '2025-02-01 00:00:00+08'
              AND paid_amount IN (12.34, 23.45)
            """
        )
        assert local_day == 12.34

        next_local_day = await business_ro.fetchval(
            """
            SELECT SUM(paid_amount)
            FROM business.gmv_v1
            WHERE region_code = 'CN_EAST'
              AND paid_at >= TIMESTAMPTZ '2025-02-01 00:00:00+08'
              AND paid_at < TIMESTAMPTZ '2025-02-02 00:00:00+08'
              AND paid_amount IN (12.34, 23.45)
            """
        )
        assert next_local_day == 23.45

        has_privileges = await business_ro.fetchrow(
            """
            SELECT
                has_table_privilege(current_user, 'business.gmv_v1', 'SELECT') AS view_select,
                has_table_privilege(current_user, 'business.gmv_v1', 'INSERT') AS view_insert,
                has_table_privilege(current_user, 'business.orders', 'SELECT') AS table_select,
                has_table_privilege(current_user, 'business.orders', 'INSERT') AS table_insert,
                has_table_privilege(current_user, 'business.order_items', 'UPDATE') AS items_update,
                has_table_privilege(current_user, 'business.metric_definitions', 'DELETE') AS metrics_delete
            """
        )
        assert dict(has_privileges) == {
            "view_select": True,
            "view_insert": False,
            "table_select": False,
            "table_insert": False,
            "items_update": False,
            "metrics_delete": False,
        }

        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await business_ro.fetch("SELECT * FROM business.orders LIMIT 1")
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await business_ro.execute(
                "INSERT INTO business.gmv_v1 (data_snapshot_id) VALUES ($1)", snapshot_id
            )
    finally:
        await business_ro.close()
