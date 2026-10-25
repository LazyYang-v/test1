"""Offline contract for the deterministic frozen business dataset."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from agentforge_core.business_dataset import (
    DATASET_ORDER_COUNT,
    GENERATOR_VERSION,
    write_dataset,
)


def test_generated_fixture_windows_have_frozen_gmv_semantics(tmp_path: Path):
    dataset = tmp_path / "business.jsonl"
    write_dataset(dataset)
    records = [json.loads(line) for line in dataset.read_bytes().splitlines()]
    orders = [record for record in records if record["record_type"] == "order"]
    fixtures = {
        "refunded": next(
            order for order in orders
            if order["status"] == "refunded" and order["paid_amount"] == "73.25"
        ),
        "unpaid": next(order for order in orders if order["ordered_at"].startswith("2025-01-23") and order["status"] == "pending"),
        "cancelled": next(order for order in orders if order["ordered_at"].startswith("2025-01-23") and order["status"] == "cancelled"),
        "boundary_start": next(order for order in orders if order["paid_at"] == "2025-01-31T15:30:00Z"),
        "boundary_end": next(order for order in orders if order["paid_at"] == "2025-01-31T16:30:00Z"),
    }

    def gmv(start: str, end: str) -> int:
        return sum(
            int(order["paid_amount"].replace(".", ""))
            for order in orders
            if order["region_code"] == "CN_EAST"
            and order["paid_at"] is not None
            and start <= order["paid_at"][:10] < end
        )

    assert gmv("2025-01-08", "2025-01-15") == 0
    assert gmv("2025-01-15", "2025-01-22") == 5_600_000
    assert gmv("2025-03-24", "2025-03-31") == 7_000_000
    assert gmv("2025-03-31", "2025-04-07") == 5_740_000
    assert fixtures["refunded"]["paid_at"] is not None
    assert fixtures["unpaid"]["paid_at"] is None
    assert fixtures["cancelled"]["paid_at"] is None
    assert fixtures["boundary_start"]["region_code"] == "CN_EAST"
    assert fixtures["boundary_start"]["paid_amount"] == "12.34"
    assert fixtures["boundary_end"]["region_code"] == "CN_EAST"
    assert fixtures["boundary_end"]["paid_amount"] == "23.45"


def test_dataset_generation_is_byte_identical_and_snapshot_id_is_sha256(tmp_path: Path):
    first = tmp_path / "first.jsonl"
    second = tmp_path / "second.jsonl"

    first_snapshot_id = write_dataset(first)
    second_snapshot_id = write_dataset(second)

    first_bytes = first.read_bytes()
    second_bytes = second.read_bytes()
    assert first_bytes == second_bytes
    assert first_snapshot_id == second_snapshot_id
    assert first_snapshot_id == hashlib.sha256(first_bytes).hexdigest()

    records = [json.loads(line) for line in first_bytes.splitlines()]
    order_keys = [
        (record["order_no"], record["record_type"])
        for record in records
        if record["record_type"] != "dataset"
    ]
    assert order_keys == sorted(order_keys)
    assert records[0] == {
        "canonicalization": "json-sort-keys-compact-utf8",
        "generator_version": GENERATOR_VERSION,
        "record_type": "dataset",
        "seed": 20261026,
        "sort_order": "order_no,record_type",
    }
    orders = [record for record in records if record["record_type"] == "order"]
    assert len(orders) == DATASET_ORDER_COUNT
    assert sum(order["status"] == "refunded" for order in orders) == 3_240
    assert {
        region: sum(order["region_code"] == region for order in orders)
        for region in {order["region_code"] for order in orders}
    } == {"CN_EAST": 36_000, "CN_NORTH": 36_000, "CN_SOUTH": 36_000}
