"""Readiness decision table: applied schema version vs the version the code expects.

Expected values come from the design baseline (migration list 0001-0010);
the decision itself is independent of where the numbers came from.
"""

import pytest

from agentforge_core.schema_version import evaluate_readiness


@pytest.mark.parametrize(
    ("expected", "applied", "decision"),
    [
        (1, None, "not_migrated"),
        (1, 1, "ok"),
        (1, 0, "version_mismatch"),
        (6, 5, "version_mismatch"),
        (5, 6, "version_mismatch"),
    ],
)
def test_evaluate_readiness_decision_table(expected, applied, decision):
    assert evaluate_readiness(expected, applied) == decision
