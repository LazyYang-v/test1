"""Canonical generator for the frozen P0 business dataset.

Run ``python -m agentforge_core.business_dataset PATH`` to write a UTF-8
JSON Lines dataset and print its SHA-256 ``data_snapshot_id``. The hash covers
all bytes in the generated file, including the manifest and final newlines.
"""

from __future__ import annotations

import argparse
import hashlib
import heapq
import json
import sys
import uuid
from collections.abc import Iterator
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

GENERATOR_VERSION = "1.0.0"
DATASET_SEED = 20261026
DATASET_ORDER_COUNT = 3 * 180 * 200
ORDERS_PER_REGION_DAY = 200
REGION_CODES = ("CN_EAST", "CN_NORTH", "CN_SOUTH")
DATASET_START = date(2025, 1, 1)
_CENTS = 100
_NAMESPACE = uuid.UUID("522b7392-e4f3-5da4-8acf-3709c042a030")
_UTC = timezone.utc

_ZERO_WINDOW_START = date(2025, 1, 8)
_ZERO_WINDOW_END = date(2025, 1, 15)
_JAN_FIXTURE_START = date(2025, 1, 15)
_JAN_FIXTURE_END = date(2025, 1, 22)
_DECLINE_BASELINE_START = date(2025, 3, 24)
_DECLINE_CURRENT_START = date(2025, 3, 31)
_DECLINE_CURRENT_END = date(2025, 4, 7)
_REFUND_FIXTURE_DATE = date(2025, 2, 10)
_UNPAID_FIXTURE_DATE = date(2025, 1, 23)
_TZ_START_DATE = date(2025, 1, 31)
_TZ_END_DATE = date(2025, 2, 1)
_REFUND_TARGET_COUNT = DATASET_ORDER_COUNT * 3 // 100


def _order_index(day: date, region_index: int, ordinal: int) -> int:
    day_index = (day - DATASET_START).days
    return (day_index * len(REGION_CODES) + region_index) * ORDERS_PER_REGION_DAY + ordinal


def _order_no(day: date, region_code: str, ordinal: int) -> str:
    return f"ORD-{region_code}-{day.isoformat()}-{ordinal:03d}"


def _stable_hash(index: int) -> bytes:
    material = f"{GENERATOR_VERSION}:{DATASET_SEED}:{index}".encode("ascii")
    return hashlib.sha256(material).digest()


def _fixture_keys() -> dict[int, str]:
    east = REGION_CODES.index("CN_EAST")
    return {
        _order_index(_REFUND_FIXTURE_DATE, east, 0): "refunded-gmv",
        _order_index(_UNPAID_FIXTURE_DATE, east, 0): "unpaid",
        _order_index(_UNPAID_FIXTURE_DATE, east, 1): "cancelled",
        _order_index(_TZ_START_DATE, east, 0): "timezone-boundary-start",
        _order_index(_TZ_END_DATE, east, 0): "timezone-boundary-end",
    }


def _zero_window_index(index: int) -> bool:
    day_index = index // (len(REGION_CODES) * ORDERS_PER_REGION_DAY)
    region_index = (index // ORDERS_PER_REGION_DAY) % len(REGION_CODES)
    day = DATASET_START + timedelta(days=day_index)
    return (
        region_index == REGION_CODES.index("CN_EAST")
        and _ZERO_WINDOW_START <= day < _ZERO_WINDOW_END
    )


def _refund_indices() -> set[int]:
    fixture_refund_index = _order_index(
        _REFUND_FIXTURE_DATE, REGION_CODES.index("CN_EAST"), 0
    )
    excluded_fixtures = set(_fixture_keys()) - {fixture_refund_index}
    eligible = (
        index
        for index in range(DATASET_ORDER_COUNT)
        if not _zero_window_index(index)
        and index != fixture_refund_index
        and index not in excluded_fixtures
    )
    selected = heapq.nsmallest(
        _REFUND_TARGET_COUNT - 1,
        eligible,
        key=lambda index: _stable_hash(index),
    )
    selected.append(fixture_refund_index)
    return set(selected)


def _money(cents: int) -> str:
    return f"{cents // _CENTS}.{cents % _CENTS:02d}"


def _iso_utc(value: datetime) -> str:
    return value.astimezone(_UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _order_and_item(
    day: date,
    region_code: str,
    region_index: int,
    ordinal: int,
    refund_indices: set[int],
    fixture_keys: dict[int, str],
) -> tuple[dict[str, Any], dict[str, Any]]:
    index = _order_index(day, region_index, ordinal)
    digest = _stable_hash(index)
    fixture_key = fixture_keys.get(index)
    order_no = _order_no(day, region_code, ordinal)
    if fixture_key is not None:
        order_no += "-A-" + fixture_key.upper().replace("-", "_")
    order_id = uuid.uuid5(_NAMESPACE, f"order:{order_no}")
    item_id = uuid.uuid5(_NAMESPACE, f"item:{order_no}:0")
    customer_id = uuid.uuid5(_NAMESPACE, f"customer:{index % 12000:05d}")

    east_fixture = region_code == "CN_EAST"
    zero_window = east_fixture and _ZERO_WINDOW_START <= day < _ZERO_WINDOW_END
    jan_gmv_fixture = east_fixture and _JAN_FIXTURE_START <= day < _JAN_FIXTURE_END
    decline_baseline = east_fixture and _DECLINE_BASELINE_START <= day < _DECLINE_CURRENT_START
    decline_current = east_fixture and _DECLINE_CURRENT_START <= day < _DECLINE_CURRENT_END

    if zero_window:
        status = "pending" if ordinal % 2 == 0 else "cancelled"
        paid_cents = 0
    elif east_fixture and day in {
        *(_ZERO_WINDOW_START + timedelta(days=offset) for offset in range(7)),
        *(_JAN_FIXTURE_START + timedelta(days=offset) for offset in range(7)),
        *(_DECLINE_BASELINE_START + timedelta(days=offset) for offset in range(7)),
        *(_DECLINE_CURRENT_START + timedelta(days=offset) for offset in range(7)),
    }:
        # These four windows have controlled paid amounts so their aggregate
        # values are exact regardless of the rest of the generated dataset.
        if jan_gmv_fixture:
            paid_cents = 4_000
        elif decline_baseline:
            paid_cents = 5_000
        elif decline_current:
            paid_cents = 4_100
        else:
            paid_cents = 0
        status = "refunded" if index in refund_indices else "paid"
    elif fixture_key == "unpaid":
        status = "pending"
        paid_cents = 0
    elif fixture_key == "cancelled":
        status = "cancelled"
        paid_cents = 0
    elif index in refund_indices:
        status = "refunded"
        paid_cents = 7_325 if fixture_key == "refunded-gmv" else 10_000 + int.from_bytes(digest[4:6], "big") % 90_001
    else:
        disposition = int.from_bytes(digest[6:8], "big") % 100
        status = "pending" if disposition < 8 else "cancelled" if disposition < 13 else "paid"
        paid_cents = 0 if status in {"pending", "cancelled"} else 10_000 + int.from_bytes(digest[4:6], "big") % 90_001

    gross_cents = paid_cents if paid_cents else 10_000 + int.from_bytes(digest[8:10], "big") % 90_001
    discount_cents = gross_cents - paid_cents
    ordered_at = datetime.combine(day, time(hour=9), _UTC) + timedelta(minutes=index % 300)
    if status in {"pending", "cancelled"}:
        paid_at = None
    else:
        paid_at = ordered_at + timedelta(hours=2)
    if fixture_key == "timezone-boundary-start":
        status = "paid"
        paid_cents = 1_234
        paid_at = datetime(2025, 1, 31, 15, 30, tzinfo=_UTC)
    elif fixture_key == "timezone-boundary-end":
        status = "paid"
        paid_cents = 2_345
        paid_at = datetime(2025, 1, 31, 16, 30, tzinfo=_UTC)
    else:
        paid_at = None if status in {"pending", "cancelled"} else ordered_at + timedelta(hours=2)

    if fixture_key in {"timezone-boundary-start", "timezone-boundary-end"}:
        gross_cents = paid_cents
        discount_cents = 0

    order = {
        "created_at": _iso_utc(ordered_at),
        "currency": "CNY",
        "customer_id": str(customer_id),
        "discount_amount": _money(discount_cents),
        "gross_amount": _money(gross_cents),
        "id": str(order_id),
        "order_no": order_no,
        "ordered_at": _iso_utc(ordered_at),
        "paid_amount": _money(paid_cents),
        "paid_at": None if paid_at is None else _iso_utc(paid_at),
        "region_code": region_code,
        "status": status,
    }
    item = {
        "category_code": f"CAT-{index % 12:02d}",
        "discount_amount": _money(discount_cents),
        "id": str(item_id),
        "order_id": str(order_id),
        "order_no": order_no,
        "paid_amount": _money(paid_cents),
        "quantity": 1,
        "sku_id": f"SKU-{index % 240:04d}",
        "unit_price": _money(gross_cents),
    }
    return order, item


def iter_dataset_records() -> Iterator[dict[str, Any]]:
    """Yield manifest, then order/item pairs in canonical stable order."""
    yield {
        "canonicalization": "json-sort-keys-compact-utf8",
        "generator_version": GENERATOR_VERSION,
        "record_type": "dataset",
        "seed": DATASET_SEED,
        "sort_order": "order_no,record_type",
    }
    refunds = _refund_indices()
    fixture_keys = _fixture_keys()

    # order_no sorts by region, then ISO date, then zero-padded ordinal.
    for region_index, region_code in sorted(enumerate(REGION_CODES), key=lambda x: x[1]):
        for day_offset in range(180):
            day = DATASET_START + timedelta(days=day_offset)
            for ordinal in range(ORDERS_PER_REGION_DAY):
                order, item = _order_and_item(
                    day,
                    region_code,
                    region_index,
                    ordinal,
                    refunds,
                    fixture_keys,
                )
                yield {"record_type": "order", **order}
                yield {"record_type": "order_item", **item}


def write_dataset(destination: str | Path) -> str:
    """Write canonical JSONL and return its SHA-256 content hash."""
    target = Path(destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    with target.open("wb") as output:
        for record in iter_dataset_records():
            line = (
                json.dumps(
                    record,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            ).encode("utf-8")
            output.write(line)
            digest.update(line)
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path, help="output JSON Lines file")
    arguments = parser.parse_args()
    snapshot_id = write_dataset(arguments.destination)
    print(snapshot_id, file=sys.stdout)


if __name__ == "__main__":
    main()
