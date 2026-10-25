"""Validate and load the canonical frozen business dataset into PostgreSQL."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import uuid
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import asyncpg

from agentforge_core.business_dataset import (
    DATASET_ORDER_COUNT,
    DATASET_SEED,
    GENERATOR_VERSION,
)

CANONICALIZATION = "json-sort-keys-compact-utf8"
SORT_ORDER = "order_no,record_type"
_BATCH_SIZE = 2_000
_ORDER_COLUMNS = (
    "id, order_no, data_snapshot_id, region_code, customer_id, ordered_at, status, "
    "gross_amount, discount_amount, paid_amount, currency, paid_at, created_at"
)
_ITEM_COLUMNS = (
    "id, order_id, sku_id, category_code, quantity, unit_price, discount_amount, paid_amount"
)


class DatasetError(ValueError):
    """Raised when dataset bytes or records do not match the frozen contract."""


def _expected_manifest() -> dict[str, Any]:
    return {
        "canonicalization": CANONICALIZATION,
        "generator_version": GENERATOR_VERSION,
        "record_type": "dataset",
        "seed": DATASET_SEED,
        "sort_order": SORT_ORDER,
    }


def _inspect(path: Path) -> tuple[str, dict[str, Any]]:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    snapshot_id = digest.hexdigest()

    with path.open("rb") as source:
        first = source.readline()
    try:
        manifest = json.loads(first)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DatasetError("dataset manifest is not valid UTF-8 JSON") from error
    if manifest != _expected_manifest():
        raise DatasetError("dataset manifest does not match the frozen generator contract")
    return snapshot_id, manifest


def _timestamp(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value.replace("Z", "+00:00"))


def _parse_order(record: dict[str, Any], snapshot_id: str) -> tuple[Any, ...]:
    return (
        uuid.UUID(record["id"]),
        record["order_no"],
        snapshot_id,
        record["region_code"],
        uuid.UUID(record["customer_id"]),
        _timestamp(record["ordered_at"]),
        record["status"],
        Decimal(record["gross_amount"]),
        Decimal(record["discount_amount"]),
        Decimal(record["paid_amount"]),
        record["currency"],
        _timestamp(record["paid_at"]),
        _timestamp(record["created_at"]),
    )


def _parse_item(record: dict[str, Any]) -> tuple[Any, ...]:
    return (
        uuid.UUID(record["id"]),
        uuid.UUID(record["order_id"]),
        record["sku_id"],
        record["category_code"],
        record["quantity"],
        Decimal(record["unit_price"]),
        Decimal(record["discount_amount"]),
        Decimal(record["paid_amount"]),
    )


async def load_dataset(conn: asyncpg.Connection, dataset_path: str | Path) -> str:
    """Verify the canonical hash, then load orders and items atomically.

    The snapshot metadata row must have been provisioned by migration 0003.
    Re-running the loader for the same snapshot is a no-op.
    """
    path = Path(dataset_path)
    snapshot_id, _ = _inspect(path)

    existing = await conn.fetchrow(
        "SELECT generator_version, seed, order_count, canonicalization, sort_order "
        "FROM business.data_snapshots WHERE data_snapshot_id = $1",
        snapshot_id,
    )
    if existing is None:
        raise DatasetError(
            "dataset snapshot is not registered; migration 0003 must seed its content hash"
        )
    if (
        existing["generator_version"] != GENERATOR_VERSION
        or existing["seed"] != DATASET_SEED
        or existing["order_count"] != DATASET_ORDER_COUNT
        or existing["canonicalization"] != CANONICALIZATION
        or existing["sort_order"] != SORT_ORDER
    ):
        raise DatasetError("registered snapshot metadata does not match generated dataset")

    order_batch: list[tuple[Any, ...]] = []
    item_batch: list[tuple[Any, ...]] = []
    order_count = 0
    item_count = 0
    order_statement = (
        f"INSERT INTO business.orders ({_ORDER_COLUMNS}) VALUES ("
        + ", ".join(f"${index}" for index in range(1, 14))
        + ")"
    )
    item_statement = (
        f"INSERT INTO business.order_items ({_ITEM_COLUMNS}) VALUES ("
        + ", ".join(f"${index}" for index in range(1, 9))
        + ")"
    )

    async with conn.transaction():
        load_state = await conn.fetchrow(
            """
            SELECT status FROM business.dataset_loads
            WHERE data_snapshot_id = $1
            FOR UPDATE
            """,
            snapshot_id,
        )
        if load_state is None:
            raise DatasetError("dataset snapshot has no load state")
        if load_state["status"] == "complete":
            return snapshot_id
        await conn.execute(
            "UPDATE business.dataset_loads SET status = 'loading' "
            "WHERE data_snapshot_id = $1",
            snapshot_id,
        )

        digest = hashlib.sha256()
        with path.open("rb") as source:
            first_line = source.readline()
            digest.update(first_line)
            try:
                loaded_manifest = json.loads(first_line)
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise DatasetError("dataset manifest is not valid UTF-8 JSON") from error
            if loaded_manifest != _expected_manifest():
                raise DatasetError("dataset manifest changed after snapshot inspection")

            for line_number, raw_line in enumerate(source, start=2):
                digest.update(raw_line)
                try:
                    line = raw_line.decode("utf-8")
                    record = json.loads(line)
                    record_type = record.pop("record_type")
                    if record_type == "order":
                        order_batch.append(_parse_order(record, snapshot_id))
                        order_count += 1
                    elif record_type == "order_item":
                        item_batch.append(_parse_item(record))
                        item_count += 1
                    else:
                        raise DatasetError(f"unknown record_type {record_type!r}")
                except (
                    UnicodeDecodeError,
                    json.JSONDecodeError,
                    KeyError,
                    TypeError,
                    ValueError,
                    ArithmeticError,
                ) as error:
                    raise DatasetError(f"invalid dataset record on line {line_number}") from error

                if len(order_batch) >= _BATCH_SIZE:
                    await conn.executemany(order_statement, order_batch)
                    order_batch.clear()
                if len(item_batch) >= _BATCH_SIZE:
                    await conn.executemany(item_statement, item_batch)
                    item_batch.clear()

        if order_batch:
            await conn.executemany(order_statement, order_batch)
        if item_batch:
            await conn.executemany(item_statement, item_batch)
        if order_count != DATASET_ORDER_COUNT or item_count != DATASET_ORDER_COUNT:
            raise DatasetError(
                f"expected {DATASET_ORDER_COUNT} orders and items, "
                f"found {order_count} orders and {item_count} items"
            )
        if digest.hexdigest() != snapshot_id:
            raise DatasetError("dataset content hash changed during load")
        await conn.execute(
            "UPDATE business.dataset_loads "
            "SET status = 'complete', completed_at = now() "
            "WHERE data_snapshot_id = $1",
            snapshot_id,
        )

    return snapshot_id


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset", type=Path, help="canonical JSON Lines business dataset")
    arguments = parser.parse_args()
    dsn = os.environ.get("AGENTFORGE_MIGRATOR_DSN")
    if not dsn:
        raise SystemExit("AGENTFORGE_MIGRATOR_DSN is required")

    async def _run() -> None:
        conn = await asyncpg.connect(dsn)
        try:
            print(await load_dataset(conn, arguments.dataset))
        finally:
            await conn.close()

    asyncio.run(_run())


if __name__ == "__main__":
    main()
