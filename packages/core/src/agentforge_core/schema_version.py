"""Schema version contract shared by the migrator and the API readiness check.

The database reports the highest applied migration version; the API refuses
to report ready unless it exactly matches the version this build expects.
"""

from typing import Literal

SCHEMA_VERSION = 9

ReadinessDecision = Literal["ok", "not_migrated", "version_mismatch"]


def evaluate_readiness(expected: int, applied: int | None) -> ReadinessDecision:
    if applied is None:
        return "not_migrated"
    if applied == expected:
        return "ok"
    return "version_mismatch"
