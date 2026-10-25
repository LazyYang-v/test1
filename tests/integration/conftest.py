"""Integration tier: real PostgreSQL via Docker.

Session scope: one ephemeral ``postgres:17`` container on a random loopback
port. It creates the one-time migrator bootstrap role via the same init script
used by Compose. Each test gets an isolated fresh database cloned from one
migrated template (or an empty database for readiness checks). Everything is
torn down: databases dropped and the container removed.
"""

from __future__ import annotations

import asyncio
import shutil
import socket
import subprocess
import time
import uuid
from collections.abc import Mapping
from pathlib import Path

import asyncpg
import pytest

from agentforge_core.migrations import apply_migrations

REPO_ROOT = Path(__file__).resolve().parents[2]
VERSIONS_DIR = REPO_ROOT / "migrations" / "versions"
INIT_SCRIPT = REPO_ROOT / "infra" / "postgres" / "init" / "01-migrator.sh"

POSTGRES_IMAGE = "postgres:17"
SUPERUSER_PASSWORD = "agentforge-it-superuser"
TEST_DB_PASSWORDS = {
    "AGENTFORGE_MIGRATOR_DB_PASSWORD": "it-migrator-pw",
    "AGENTFORGE_API_DB_PASSWORD": "it-api-pw",
    "AGENTFORGE_WORKER_DB_PASSWORD": "it-worker-pw",
    "AGENTFORGE_BUSINESS_RO_DB_PASSWORD": "it-business-ro-pw",
    "AGENTFORGE_EVAL_DB_PASSWORD": "it-eval-pw",
}
ROLE_USERS = {
    "migrator": "agentforge_migrator",
    "api": "agentforge_api",
    "worker": "agentforge_worker",
    "business_ro": "agentforge_business_ro",
    "eval": "agentforge_eval",
}
ROLE_PASSWORDS = {
    "migrator": TEST_DB_PASSWORDS["AGENTFORGE_MIGRATOR_DB_PASSWORD"],
    "api": TEST_DB_PASSWORDS["AGENTFORGE_API_DB_PASSWORD"],
    "worker": TEST_DB_PASSWORDS["AGENTFORGE_WORKER_DB_PASSWORD"],
    "business_ro": TEST_DB_PASSWORDS["AGENTFORGE_BUSINESS_RO_DB_PASSWORD"],
    "eval": TEST_DB_PASSWORDS["AGENTFORGE_EVAL_DB_PASSWORD"],
}


def run_async(coro):
    return asyncio.run(coro)


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    probe = subprocess.run(["docker", "info"], capture_output=True, timeout=30)
    return probe.returncode == 0


def _wait_ready(container: str) -> None:
    # Probe TCP, not the Unix socket: during first-boot init the official
    # image runs a temporary server that only listens on the socket.
    for _ in range(120):
        probe = subprocess.run(
            [
                "docker",
                "exec",
                container,
                "pg_isready",
                "-h",
                "127.0.0.1",
                "-U",
                "postgres",
                "-d",
                "agentforge",
            ],
            capture_output=True,
        )
        if probe.returncode == 0:
            return
        time.sleep(0.5)
    raise RuntimeError(f"postgres container {container} did not become ready")


@pytest.fixture(scope="session")
def postgres_server():
    if not _docker_available():
        pytest.skip("integration tests require a running Docker daemon")

    container = f"agentforge-it-{uuid.uuid4().hex[:12]}"
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]

    subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "-d",
            "--name",
            container,
            "-e",
            f"POSTGRES_PASSWORD={SUPERUSER_PASSWORD}",
            "-e",
            "POSTGRES_USER=postgres",
            "-e",
            "POSTGRES_DB=agentforge",
            "-e",
            f"AGENTFORGE_MIGRATOR_DB_PASSWORD={TEST_DB_PASSWORDS['AGENTFORGE_MIGRATOR_DB_PASSWORD']}",
            "-p",
            f"127.0.0.1:{port}:5432",
            "-v",
            f"{INIT_SCRIPT}:/docker-entrypoint-initdb.d/01-migrator.sh:ro",
            POSTGRES_IMAGE,
        ],
        check=True,
        capture_output=True,
    )

    host = f"127.0.0.1:{port}"
    # Runtime integration DB creation and teardown are delegated to the cluster
    # admin, while all migration SQL still connects as the migrator role.
    server = {
        "container": container,
        "host": host,
        "superuser_dsn_prefix": f"postgresql://postgres:{SUPERUSER_PASSWORD}@{host}",
    }
    try:
        _wait_ready(container)
        yield {
            **server,
            "superuser_dsn": f"{server['superuser_dsn_prefix']}/postgres",
        }
    finally:
        subprocess.run(["docker", "rm", "-f", container], capture_output=True)


async def _create_database_async(
    server: dict, name: str, owner: str = "agentforge_migrator"
) -> None:
    conn = await asyncpg.connect(f"{server['superuser_dsn_prefix']}/postgres")
    try:
        sql = await conn.fetchval(
            "SELECT format('CREATE DATABASE %I OWNER %I', $1::text, $2::text)",
            name,
            owner,
        )
        await conn.execute(sql)
    finally:
        await conn.close()


async def _clone_database_async(server: dict, source: str, target: str) -> None:
    conn = await asyncpg.connect(f"{server['superuser_dsn_prefix']}/postgres")
    try:
        sql = await conn.fetchval(
            "SELECT format("
            "'CREATE DATABASE %I TEMPLATE %I OWNER %I', $1::text, $2::text, $3::text)",
            target,
            source,
            "agentforge_migrator",
        )
        await conn.execute(sql)
    finally:
        await conn.close()


async def _drop_database_async(server: dict, name: str) -> None:
    conn = await asyncpg.connect(f"{server['superuser_dsn_prefix']}/postgres")
    try:
        sql = await conn.fetchval(
            "SELECT format('DROP DATABASE %I WITH (FORCE)', $1::text)", name
        )
        await conn.execute(sql)
    finally:
        await conn.close()


async def _migrate_database_async(server: dict, name: str, env: Mapping[str, str]) -> None:
    migrator_dsn = (
        f"postgresql://agentforge_migrator:"
        f"{env['AGENTFORGE_MIGRATOR_DB_PASSWORD']}@{server['host']}/{name}"
    )
    await apply_migrations(migrator_dsn, VERSIONS_DIR, env)


def _create_database(
    server: dict, name: str, owner: str = "agentforge_migrator"
) -> None:
    run_async(_create_database_async(server, name, owner))


def _clone_database(server: dict, source: str, target: str) -> None:
    run_async(_clone_database_async(server, source, target))


def _drop_database(server: dict, name: str) -> None:
    run_async(_drop_database_async(server, name))


def _migrate_database(server: dict, name: str, env: Mapping[str, str]) -> None:
    run_async(_migrate_database_async(server, name, env))


@pytest.fixture(scope="session")
def migrated_template_db(postgres_server):
    """One migrated template database for this integration session."""
    name = f"agentforge_template_{uuid.uuid4().hex[:8]}"
    _create_database(postgres_server, name, owner="agentforge_migrator")
    try:
        _migrate_database(postgres_server, name, TEST_DB_PASSWORDS)
        yield name
    finally:
        _drop_database(postgres_server, name)


@pytest.fixture
def migrated_db(postgres_server, migrated_template_db):
    """A fresh database cloned from the session's migrated template."""
    name = f"agentforge_it_{uuid.uuid4().hex[:8]}"
    _clone_database(postgres_server, migrated_template_db, name)
    try:
        yield {
            "name": name,
            "server": postgres_server,
            "superuser_dsn": f"{postgres_server['superuser_dsn_prefix']}/{name}",
            "role_dsn": lambda role: (
                f"postgresql://{ROLE_USERS[role]}:{ROLE_PASSWORDS[role]}"
                f"@{postgres_server['host']}/{name}"
            ),
            "migrate_env": TEST_DB_PASSWORDS,
        }
    finally:
        _drop_database(postgres_server, name)


@pytest.fixture
def unmigrated_db(postgres_server):
    """A fresh database that has never had a migration applied."""
    name = f"agentforge_it_{uuid.uuid4().hex[:8]}"
    _create_database(postgres_server, name, owner="agentforge_migrator")
    try:
        yield {
            "name": name,
            "server": postgres_server,
            "superuser_dsn": f"{postgres_server['superuser_dsn_prefix']}/{name}",
            "role_dsn": lambda role: (
                f"postgresql://{ROLE_USERS[role]}:{ROLE_PASSWORDS[role]}"
                f"@{postgres_server['host']}/{name}"
            ),
        }
    finally:
        _drop_database(postgres_server, name)
