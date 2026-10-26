"""Shared fixtures for the ticket-06 worker integration seam."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Any

import asyncpg
from agentforge_core.business_dataset import write_dataset
from agentforge_core.business_loader import load_dataset
from agentforge_core.schema_version import SCHEMA_VERSION  # noqa: F401
from httpx import ASGITransport, AsyncClient

from agentforge_api.app import create_app
from agentforge_api.auth import create_access_token
from agentforge_api.config import Settings

JWT_SECRET = "integration-test-signing-secret-at-least-32-bytes"
USER_ID = "0192a3b4-c5d6-7e8f-9012-3456789abcde"
TOOL_ID = "00000000-0000-7000-8000-000000000601"
SNAPSHOT_ID = "e38d3afb175b4d130db4e1600cef8710b448bd67151770f1ca6ad6734e0ab5b8"

FROZEN_DATE_RANGE = {
    "start": "2025-03-31",
    "end_exclusive": "2025-04-07",
    "timezone": "UTC",
}


_FROZEN_DATASET_DIR: tempfile.TemporaryDirectory[str] | None = None


def _frozen_dataset_path() -> Path:
    """Build the canonical frozen dataset once per test process."""
    global _FROZEN_DATASET_DIR
    if _FROZEN_DATASET_DIR is None:
        _FROZEN_DATASET_DIR = tempfile.TemporaryDirectory(
            prefix="agentforge-frozen-dataset-"
        )
        destination = Path(_FROZEN_DATASET_DIR.name) / "business.jsonl"
        snapshot_id = write_dataset(destination)
        assert snapshot_id == SNAPSHOT_ID
    return Path(_FROZEN_DATASET_DIR.name) / "business.jsonl"


async def complete_dataset_load(db: dict) -> None:
    """Load the real frozen dataset through the canonical loader.

    Ticket 07 executes real SQL over ``business.gmv_v1``, so the worker seam
    must run against real business rows rather than a state-only fixture.
    """
    conn = await asyncpg.connect(db["role_dsn"]("migrator"))
    try:
        assert await load_dataset(conn, _frozen_dataset_path()) == SNAPSHOT_ID
    finally:
        await conn.close()


async def seed_agent(
    db: dict,
    *,
    agent_key: str = "sales-report",
    bind_tool: bool = True,
    grants: tuple[str, ...] = ("CN_EAST",),
    published: bool = True,
    profile_timeout_ms: int = 2500,
) -> dict[str, str]:
    suffix = uuid.uuid4().hex[:8]
    profile_id = str(uuid.uuid4())
    agent_id = str(uuid.uuid4())
    conn = await asyncpg.connect(db["role_dsn"]("migrator"))
    try:
        await conn.execute(
            """
            INSERT INTO identity.users (id, email, display_name, status)
            VALUES ($1::uuid, 'worker-user@example.com', 'Worker User', 'active')
            ON CONFLICT (id) DO NOTHING
            """,
            USER_ID,
        )
        for index, region in enumerate(grants, start=1):
            await conn.execute(
                """
                INSERT INTO identity.region_grants (id, user_id, region_code, permission)
                VALUES ($1::uuid, $2::uuid, $3, 'read')
                ON CONFLICT DO NOTHING
                """,
                str(uuid.uuid5(uuid.NAMESPACE_URL, f"worker-grant:{suffix}:{index}")),
                USER_ID,
                region,
            )
        await conn.execute(
            """
            INSERT INTO registry.model_profiles
                (id, profile_key, provider, model_name, endpoint_alias,
                 credential_ref, timeout_ms, max_output_tokens)
            VALUES ($1::uuid, $2, 'fake', 'fake-v1', 'fake-local',
                    'secret://agentforge/test/fake-provider', $3, 768)
            """,
            profile_id,
            f"{agent_key}-{suffix}",
            profile_timeout_ms,
        )
        await conn.execute(
            """
            INSERT INTO registry.agent_versions
                (id, agent_key, version, name, graph_key, model_profile_id,
                 config_hash, status)
            VALUES ($1::uuid, $2, '1.0.0', 'Sales report', 'metrics-v1',
                    $3::uuid, $4, 'draft')
            """,
            agent_id,
            agent_key,
            profile_id,
            "a" * 64,
        )
        if bind_tool:
            await conn.execute(
                "INSERT INTO registry.agent_tool_bindings "
                "(agent_version_id, tool_version_id) VALUES ($1::uuid, $2::uuid)",
                agent_id,
                TOOL_ID,
            )
        if published:
            await conn.execute(
                """
                UPDATE registry.agent_versions
                SET status = 'published', model_config_snapshot = '{"model":"fake-v1"}',
                    published_at = now()
                WHERE id = $1::uuid
                """,
                agent_id,
            )
    finally:
        await conn.close()
    return {"agent_id": agent_id, "agent_key": agent_key, "profile_id": profile_id}


def registry_cli(db: dict, *args: str) -> dict[str, Any]:
    """Run the real Registry CLI, which freezes the profile into a snapshot."""
    env = os.environ.copy()
    env["AGENTFORGE_REGISTRY_DSN"] = db["role_dsn"]("registry_cli")
    executable = db["repo_root"] / ".venv" / "bin" / "agentforge-registry"
    process = subprocess.run(
        [str(executable), *args],
        cwd=db["repo_root"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert process.returncode == 0, process.stderr
    return json.loads(process.stdout)


def token(user_id: str = USER_ID) -> str:
    return create_access_token(
        user_id=user_id,
        token_version=1,
        secret=JWT_SECRET,
        ttl_seconds=900,
    )[0]


def app_for(db: dict):
    return create_app(
        Settings(api_db_dsn=db["role_dsn"]("api"), jwt_secret=JWT_SECRET)
    )


def create_task_body(
    agent_key: str = "sales-report",
    *,
    region_codes: list[str] | None = None,
    question: str = "Explain the change in CN_EAST GMV from the previous period",
    date_range: dict[str, str] | None = None,
) -> dict[str, Any]:
    return {
        "agent_key": agent_key,
        "task_type": "business_analysis",
        "input": {
            "question": question,
            "region_codes": region_codes or ["CN_EAST"],
            "date_range": dict(date_range or FROZEN_DATE_RANGE),
            "compare_mode": "previous_period",
        },
        "execution": {"timeout_seconds": 60, "max_attempts": 1},
    }


async def create_task_via_api(
    db: dict,
    agent_key: str = "sales-report",
    *,
    idempotency_key: str | None = None,
    region_codes: list[str] | None = None,
    date_range: dict[str, str] | None = None,
) -> str:
    headers = {
        "Authorization": f"Bearer {token()}",
        "Idempotency-Key": idempotency_key or f"worker-task-{uuid.uuid4()}",
    }
    async with AsyncClient(
        transport=ASGITransport(app=app_for(db)), base_url="http://testserver"
    ) as client:
        response = await client.post(
            "/api/v1/tasks",
            json=create_task_body(
                agent_key, region_codes=region_codes, date_range=date_range
            ),
            headers=headers,
        )
    assert response.status_code == 202, response.text
    return response.json()["task_id"]


async def get_task_via_api(db: dict, task_id: str) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {token()}"}
    async with AsyncClient(
        transport=ASGITransport(app=app_for(db)), base_url="http://testserver"
    ) as client:
        response = await client.get(f"/api/v1/tasks/{task_id}", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


async def fetch_task(db: dict, task_id: str) -> dict[str, Any]:
    conn = await asyncpg.connect(db["role_dsn"]("migrator"))
    try:
        row = await conn.fetchrow(
            """
            SELECT id::text AS task_id, status, current_attempt_no, result_json,
                   error_code, error_message, completed_at, data_snapshot_id
            FROM runtime.tasks WHERE id = $1::uuid
            """,
            task_id,
        )
    finally:
        await conn.close()
    return dict(row)


async def fetch_events(db: dict, task_id: str) -> list[dict[str, Any]]:
    conn = await asyncpg.connect(db["role_dsn"]("migrator"))
    try:
        rows = await conn.fetch(
            """
            SELECT task_event_seq, event_type, payload_json, attempt_id::text AS attempt_id
            FROM runtime.task_events WHERE task_id = $1::uuid
            ORDER BY task_event_seq
            """,
            task_id,
        )
    finally:
        await conn.close()
    return [
        {
            "seq": row["task_event_seq"],
            "type": row["event_type"],
            "payload": json.loads(row["payload_json"]),
            "attempt_id": row["attempt_id"],
        }
        for row in rows
    ]


async def fetch_steps(db: dict, task_id: str) -> list[dict[str, Any]]:
    conn = await asyncpg.connect(db["role_dsn"]("migrator"))
    try:
        rows = await conn.fetch(
            """
            SELECT step_key, graph_node_key, status, result_json, result_hash,
                   owning_attempt_id::text AS owning_attempt_id, fencing_epoch
            FROM runtime.step_executions WHERE task_id = $1::uuid
            """,
            task_id,
        )
    finally:
        await conn.close()
    return [
        {
            **dict(row),
            "result": None if row["result_json"] is None else json.loads(row["result_json"]),
        }
        for row in rows
    ]


async def fetch_attempts(db: dict, task_id: str) -> list[dict[str, Any]]:
    conn = await asyncpg.connect(db["role_dsn"]("migrator"))
    try:
        rows = await conn.fetch(
            """
            SELECT id::text AS id, attempt_no, status, worker_id, lease_token::text,
                   fencing_epoch, lease_expires_at, heartbeat_at, finished_at,
                   error_code
            FROM runtime.task_attempts WHERE task_id = $1::uuid
            ORDER BY attempt_no
            """,
            task_id,
        )
    finally:
        await conn.close()
    return [dict(row) for row in rows]


async def fetch_evidences(db: dict, task_id: str) -> list[dict[str, Any]]:
    conn = await asyncpg.connect(db["role_dsn"]("migrator"))
    try:
        rows = await conn.fetch(
            """
            SELECT id::text AS evidence_id, task_id::text AS task_id,
                   data_snapshot_id, metric_key, metric_version, unit,
                   current_value, previous_value, absolute_change, change_rate,
                   row_count, query_plan_json, result_json, result_set_hash
            FROM harness.evidences WHERE task_id = $1::uuid
            ORDER BY created_at, id
            """,
            task_id,
        )
    finally:
        await conn.close()
    return [
        {
            **dict(row),
            "data_snapshot_id": row["data_snapshot_id"].strip(),
            "query_plan": json.loads(row["query_plan_json"]),
            "result": json.loads(row["result_json"]),
            "result_set_hash": row["result_set_hash"].strip(),
        }
        for row in rows
    ]


async def fetch_audits(db: dict, *, action: str) -> list[dict[str, Any]]:
    conn = await asyncpg.connect(db["role_dsn"]("migrator"))
    try:
        rows = await conn.fetch(
            """
            SELECT decision, reason_code, metadata_json, resource_id
            FROM audit.audit_events WHERE action = $1 ORDER BY id
            """,
            action,
        )
    finally:
        await conn.close()
    return [
        {**dict(row), "metadata": json.loads(row["metadata_json"])} for row in rows
    ]


async def wait_until(predicate, *, timeout: float = 10.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)
