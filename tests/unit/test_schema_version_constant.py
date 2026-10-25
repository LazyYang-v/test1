"""The API's expected schema version must track the shipped migration files.

Drift guard: bumping SCHEMA_VERSION without adding a migration (or vice
versa) fails here instead of silently breaking readiness.
"""

from pathlib import Path

from agentforge_core.schema_version import SCHEMA_VERSION

VERSIONS_DIR = Path(__file__).resolve().parents[2] / "migrations" / "versions"


def test_expected_schema_version_matches_highest_shipped_migration():
    versions = sorted(
        int(path.name.split("_", 1)[0]) for path in VERSIONS_DIR.glob("*.sql")
    )
    assert versions, "migrations/versions must contain at least one migration"
    assert SCHEMA_VERSION == versions[-1]
