"""Migration files are applied in numeric order."""

from pathlib import Path

from agentforge_core.migrations import _discover


def test_migrations_are_discovered_in_numeric_order(tmp_path: Path):
    (tmp_path / "0002_second.sql").write_text("SELECT 2;")
    (tmp_path / "0001_first.sql").write_text("SELECT 1;")

    migrations = _discover(tmp_path)

    assert [version for version, _, _ in migrations] == [1, 2]
    assert [path.name for _, _, path in migrations] == [
        "0001_first.sql",
        "0002_second.sql",
    ]
