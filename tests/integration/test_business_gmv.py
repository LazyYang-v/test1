"""Frozen GMV and business_ro behavior against real PostgreSQL."""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from pathlib import Path

import asyncpg
import pytest

from agentforge_core import business_loader
from agentforge_core.business_dataset import write_dataset
from agentforge_core.business_loader import DatasetError, load_dataset

pytestmark = pytest.mark.integration


async def test_changed_dataset_bytes_roll_back_all_loaded_rows(
    migrated_db, tmp_path: Path, monkeypatch
):
    dataset_path = tmp_path / "business.jsonl"
    snapshot_id = write_dataset(dataset_path)
    inspect = business_loader._inspect

    def inspect_then_change(path: Path):
        inspected_snapshot, manifest = inspect(path)
        loop = asyncio.get_running_loop()

        def replace_after_inspection():
            records = dataset_path.read_text(encoding="utf-8").splitlines()
            record = json.loads(records[1])
            record["region_code"] = (
                "CN_NORTH" if record["region_code"] != "CN_NORTH" else "CN_EAST"
            )
            records[1] = json.dumps(
                record, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            dataset_path.write_text("\n".join(records) + "\n", encoding="utf-8")

        loop.call_soon(replace_after_inspection)
        return inspected_snapshot, manifest

    monkeypatch.setattr(business_loader, "_inspect", inspect_then_change)
    migrator = await asyncpg.connect(migrated_db["role_dsn"]("migrator"))
    try:
        with pytest.raises(DatasetError, match="hash"):
            await load_dataset(migrator, dataset_path)
        counts = await migrator.fetchrow(
            """
            SELECT
                (SELECT count(*) FROM business.orders WHERE data_snapshot_id = $1) AS orders,
                (SELECT count(*) FROM business.order_items oi
                 JOIN business.orders o ON o.id = oi.order_id
                 WHERE o.data_snapshot_id = $1) AS items
            """,
            snapshot_id,
        )
        assert dict(counts) == {"orders": 0, "items": 0}
    finally:
        await migrator.close()


async def test_loaded_business_snapshot_is_immutable_and_business_ro_queries_frozen_gmv(
    migrated_db, tmp_path: Path
):
    dataset_path = tmp_path / "business.jsonl"
    snapshot_id = write_dataset(dataset_path)
    migrator = await asyncpg.connect(migrated_db["role_dsn"]("migrator"))
    try:
        await load_dataset(migrator, dataset_path)
        assert await load_dataset(migrator, dataset_path) == snapshot_id
        with pytest.raises(asyncpg.PostgresError, match="cannot be reset"):
            await migrator.execute(
                "UPDATE business.dataset_loads SET status = 'loading' "
                "WHERE data_snapshot_id = $1",
                snapshot_id,
            )
        mutation_statements = (
            (
                """
                INSERT INTO business.orders (
                    id, order_no, data_snapshot_id, region_code, customer_id, ordered_at,
                    status, gross_amount, discount_amount, paid_amount, currency, paid_at,
                    created_at
                )
                SELECT gen_random_uuid(), order_no || '-EXTRA', data_snapshot_id,
                       region_code, customer_id, ordered_at, status, gross_amount,
                       discount_amount, paid_amount, currency, paid_at, created_at
                FROM business.orders LIMIT 1
                """,
                (),
            ),
            (
                "UPDATE business.orders SET region_code = region_code WHERE data_snapshot_id = $1",
                (snapshot_id,),
            ),
            (
                "DELETE FROM business.orders WHERE data_snapshot_id = $1",
                (snapshot_id,),
            ),
            (
                """
                INSERT INTO business.order_items (
                    id, order_id, sku_id, category_code, quantity, unit_price,
                    discount_amount, paid_amount
                )
                SELECT gen_random_uuid(), order_id, sku_id || '-EXTRA', category_code,
                       quantity, unit_price, discount_amount, paid_amount
                FROM business.order_items LIMIT 1
                """,
                (),
            ),
            ("UPDATE business.order_items SET sku_id = sku_id WHERE true", ()),
            ("DELETE FROM business.order_items WHERE true", ()),
        )
        for statement, arguments in mutation_statements:
            with pytest.raises(asyncpg.PostgresError, match="frozen|immutable"):
                await migrator.execute(statement, *arguments)
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
        change_rate = (
            east_totals["decline_current"] - east_totals["decline_baseline"]
        ) / east_totals["decline_baseline"]
        assert change_rate == Decimal("-0.18")

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
        assert local_day == Decimal("12.34")

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
        assert next_local_day == Decimal("23.45")

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
