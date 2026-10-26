"""Task creation behavior through the HTTP API and real PostgreSQL."""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime

import asyncpg
import pytest
from agentforge_core.business_dataset import write_dataset
from agentforge_core.business_loader import load_dataset
from httpx import ASGITransport, AsyncClient

from agentforge_api.app import create_app
from agentforge_api.auth import create_access_token
from agentforge_api.config import Settings

pytestmark = pytest.mark.integration
JWT_SECRET = "integration-test-signing-secret-at-least-32-bytes"
USER_ID = "0192a3b4-c5d6-7e8f-9012-3456789abcde"
PROFILE_ID = "0192a3b4-c5d6-7e8f-9012-3456789abd01"
AGENT_VERSION_ID = "0192a3b4-c5d6-7e8f-9012-3456789abd02"


async def _seed_user_and_agent(migrated_db, *, grant_regions=("east",)):
    conn = await asyncpg.connect(migrated_db["role_dsn"]("migrator"))
    try:
        await conn.execute(
            """
            INSERT INTO identity.users (id, email, display_name, status)
            VALUES ($1::uuid, 'task-user@example.com', 'Task User', 'active')
            """,
            USER_ID,
        )
        for index, region in enumerate(grant_regions, start=1):
            await conn.execute(
                """
                INSERT INTO identity.region_grants (id, user_id, region_code, permission)
                VALUES ($1::uuid, $2::uuid, $3, 'read')
                """,
                str(uuid.uuid5(uuid.NAMESPACE_URL, f"task-grant:{region}:{index}")),
                USER_ID,
                region,
            )
        await conn.execute(
            """
            INSERT INTO registry.model_profiles
                (id, profile_key, provider, model_name, endpoint_alias,
                 credential_ref, timeout_ms, max_output_tokens)
            VALUES ($1::uuid, 'task-fake', 'fake', 'fake-v1', 'fake-local',
                    'secret://agentforge/test/fake-provider', 2500, 768)
            """,
            PROFILE_ID,
        )
        await conn.execute(
            """
            INSERT INTO registry.agent_versions
                (id, agent_key, version, name, graph_key, model_profile_id,
                 config_hash, status)
            VALUES ($1::uuid, 'sales-report', '1.0.0', 'Sales report', 'metrics-v1',
                    $2::uuid, $3, 'draft')
            """,
            AGENT_VERSION_ID,
            PROFILE_ID,
            "a" * 64,
        )
        await conn.execute(
            """
            UPDATE registry.agent_versions
            SET status = 'published', model_config_snapshot = '{"model":"fake-v1"}',
                published_at = now()
            WHERE id = $1::uuid
            """,
            AGENT_VERSION_ID,
        )
    finally:
        await conn.close()
    return USER_ID


def _token(user_id=USER_ID):
    return create_access_token(
        user_id=user_id,
        token_version=1,
        secret=JWT_SECRET,
        ttl_seconds=900,
    )[0]


def _app(migrated_db):
    return create_app(
        Settings(api_db_dsn=migrated_db["role_dsn"]("api"), jwt_secret=JWT_SECRET)
    )


def _request_body(**overrides):
    body = {
        "agent_key": "sales-report",
        "task_type": "analysis",
        "input": {
            "question": "Summarize east region GMV",
            "region_codes": ["east"],
            "date_range": {
                "start": "2026-10-01T00:00:00Z",
                "end": "2026-10-08T00:00:00Z",
            },
            "compare_mode": "previous_period",
        },
        "execution": {},
    }
    body.update(overrides)
    return body


async def test_task_creation_returns_accepted_and_persists_task_and_created_event(
    migrated_db, tmp_path
):
    await _seed_user_and_agent(migrated_db)
    dataset_path = tmp_path / "business.jsonl"
    write_dataset(dataset_path)
    migrator = await asyncpg.connect(migrated_db["role_dsn"]("migrator"))
    try:
        await load_dataset(migrator, dataset_path)
    finally:
        await migrator.close()
    async with AsyncClient(
        transport=ASGITransport(app=_app(migrated_db)), base_url="http://testserver"
    ) as client:
        response = await client.post(
            "/api/v1/tasks",
            json=_request_body(),
            headers={
                "Authorization": f"Bearer {_token()}",
                "Idempotency-Key": "create-task-001",
                "X-Request-ID": "task-create-request-001",
            },
        )

    assert response.status_code == 202
    payload = response.json()
    assert payload["status"] == "queued"
    task_id = payload["task_id"]
    assert payload["links"]["self"] == f"/api/v1/tasks/{task_id}"

    conn = await asyncpg.connect(migrated_db["role_dsn"]("migrator"))
    try:
        task = await conn.fetchrow(
            """
            SELECT owner_user_id::text AS owner_user_id,
                   agent_version_id::text AS agent_version_id, status,
                   input_json, region_scope_json, authorization_snapshot,
                   data_snapshot_id, current_attempt_no
            FROM runtime.tasks WHERE id = $1::uuid
            """,
            task_id,
        )
        event = await conn.fetchrow(
            """
            SELECT task_event_seq, event_type, trace_id
            FROM runtime.task_events WHERE task_id = $1::uuid
            """,
            task_id,
        )
        audit = await conn.fetchrow(
            """
            SELECT action, resource_id, decision FROM audit.audit_events
            WHERE action = 'task.created' AND resource_id = $1
            """,
            task_id,
        )
    finally:
        await conn.close()

    assert task["owner_user_id"] == USER_ID
    assert task["agent_version_id"] == AGENT_VERSION_ID
    assert task["status"] == "queued"
    assert task["region_scope_json"] == '["east"]'
    assert json.loads(task["authorization_snapshot"]) == [
        {"region_code": "east", "permission": "read"}
    ]
    assert event["task_event_seq"] == 1
    assert event["event_type"] == "task.created"
    assert event["trace_id"] == "task-create-request-001"
    assert (audit["action"], audit["resource_id"], audit["decision"]) == (
        "task.created",
        task_id,
        "allowed",
    )
    assert task["data_snapshot_id"].strip() == (
        "e38d3afb175b4d130db4e1600cef8710b448bd67151770f1ca6ad6734e0ab5b8"
    )
    assert task["current_attempt_no"] == 0


async def test_task_creation_atomicity_and_response_replay(
    migrated_db, tmp_path
):
    await _seed_user_and_agent(migrated_db)
    dataset_path = tmp_path / "business.jsonl"
    write_dataset(dataset_path)
    migrator = await asyncpg.connect(migrated_db["role_dsn"]("migrator"))
    try:
        await load_dataset(migrator, dataset_path)
    finally:
        await migrator.close()

    async def fail_task_created_audit(conn):
        await conn.execute(
            """
            CREATE OR REPLACE FUNCTION audit.fail_task_created_audit()
            RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                IF NEW.action = 'task.created' THEN
                    RAISE EXCEPTION 'injected audit failure';
                END IF;
                RETURN NEW;
            END;
            $$;
            CREATE TRIGGER fail_task_created_audit
            BEFORE INSERT ON audit.audit_events
            FOR EACH ROW EXECUTE FUNCTION audit.fail_task_created_audit()
            """
        )

    migrator = await asyncpg.connect(migrated_db["role_dsn"]("migrator"))
    try:
        await fail_task_created_audit(migrator)
    finally:
        await migrator.close()

    app = _app(migrated_db)
    headers = {
        "Authorization": f"Bearer {_token()}",
        "Idempotency-Key": "atomic-task-001",
    }
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://testserver",
    ) as client:
        failed = await client.post(
            "/api/v1/tasks",
            json=_request_body(),
            headers={**headers, "X-Request-ID": "task-atomic-failure"},
        )
    assert failed.status_code == 500
    assert failed.json() == {
        "error": {
            "code": "INTERNAL_ERROR",
            "message": "The request could not be completed",
            "retryable": False,
            "details": {},
        },
        "request_id": "task-atomic-failure",
    }

    conn = await asyncpg.connect(migrated_db["role_dsn"]("migrator"))
    try:
        await conn.execute("DROP TRIGGER fail_task_created_audit ON audit.audit_events")
        await conn.execute("DROP FUNCTION audit.fail_task_created_audit()")
    finally:
        await conn.close()
    conn = await asyncpg.connect(migrated_db["role_dsn"]("migrator"))
    try:
        before_retry = await conn.fetchrow(
            """
            SELECT (SELECT count(*) FROM runtime.tasks) AS tasks,
                   (SELECT count(*) FROM runtime.task_events) AS events,
                   (SELECT count(*) FROM runtime.idempotency_records) AS idempotency
            """
        )
    finally:
        await conn.close()
    assert dict(before_retry) == {"tasks": 0, "events": 0, "idempotency": 0}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        created = await client.post("/api/v1/tasks", json=_request_body(), headers=headers)
        replayed = await client.post("/api/v1/tasks", json=_request_body(), headers=headers)
    assert created.status_code == replayed.status_code == 202
    assert created.json() == replayed.json()


async def test_same_key_replays_normalized_request_even_after_grant_revocation(
    migrated_db, tmp_path
):
    await _seed_user_and_agent(migrated_db)
    dataset_path = tmp_path / "business.jsonl"
    write_dataset(dataset_path)
    migrator = await asyncpg.connect(migrated_db["role_dsn"]("migrator"))
    try:
        await load_dataset(migrator, dataset_path)
    finally:
        await migrator.close()

    headers = {
        "Authorization": f"Bearer {_token()}",
        "Idempotency-Key": "normalized-replay-001",
    }
    app = _app(migrated_db)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        first = await client.post("/api/v1/tasks", json=_request_body(), headers=headers)

        migrator = await asyncpg.connect(migrated_db["role_dsn"]("migrator"))
        try:
            await migrator.execute(
                "UPDATE identity.region_grants SET revoked_at = now() WHERE user_id = $1::uuid",
                USER_ID,
            )
        finally:
            await migrator.close()

        equivalent = _request_body(
            input={
                **_request_body()["input"],
                "region_codes": [" east "],
                "date_range": {
                    "start": "2026-10-01T08:00:00+08:00",
                    "end": "2026-10-08T08:00:00+08:00",
                },
            }
        )
        replay = await client.post("/api/v1/tasks", json=equivalent, headers=headers)

    assert first.status_code == replay.status_code == 202
    assert replay.json() == first.json()


async def test_same_key_different_request_conflicts_without_second_task(
    migrated_db, tmp_path
):
    await _seed_user_and_agent(migrated_db)
    dataset_path = tmp_path / "business.jsonl"
    write_dataset(dataset_path)
    migrator = await asyncpg.connect(migrated_db["role_dsn"]("migrator"))
    try:
        await load_dataset(migrator, dataset_path)
    finally:
        await migrator.close()

    headers = {
        "Authorization": f"Bearer {_token()}",
        "Idempotency-Key": "conflicting-request-001",
    }
    async with AsyncClient(
        transport=ASGITransport(app=_app(migrated_db)), base_url="http://testserver"
    ) as client:
        first = await client.post("/api/v1/tasks", json=_request_body(), headers=headers)
        conflicting = _request_body(
            input={**_request_body()["input"], "question": "A different question"}
        )
        second = await client.post("/api/v1/tasks", json=conflicting, headers=headers)

    assert first.status_code == 202
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    conn = await asyncpg.connect(migrated_db["role_dsn"]("migrator"))
    try:
        count = await conn.fetchval("SELECT count(*) FROM runtime.tasks")
    finally:
        await conn.close()
    assert count == 1


async def test_task_list_filters_and_paginates_with_stable_order(migrated_db, tmp_path):
    await _seed_user_and_agent(migrated_db)
    dataset_path = tmp_path / "business.jsonl"
    write_dataset(dataset_path)
    migrator = await asyncpg.connect(migrated_db["role_dsn"]("migrator"))
    try:
        await load_dataset(migrator, dataset_path)
    finally:
        await migrator.close()

    app = _app(migrated_db)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        task_ids = []
        for index in range(3):
            response = await client.post(
                "/api/v1/tasks",
                json=_request_body(),
                headers={
                    "Authorization": f"Bearer {_token()}",
                    "Idempotency-Key": f"pagination-task-{index}",
                },
            )
            assert response.status_code == 202
            task_ids.append(response.json()["task_id"])

        migrator = await asyncpg.connect(migrated_db["role_dsn"]("migrator"))
        try:
            await migrator.execute(
                "UPDATE runtime.tasks SET created_at = $2::timestamptz "
                "WHERE id = $1::uuid",
                task_ids[0], datetime(2026, 10, 1, tzinfo=UTC),
            )
            await migrator.execute(
                "UPDATE runtime.tasks SET created_at = $2::timestamptz, status = 'failed' "
                "WHERE id = $1::uuid",
                task_ids[1], datetime(2026, 10, 2, tzinfo=UTC),
            )
            await migrator.execute(
                "UPDATE runtime.tasks SET created_at = $2::timestamptz "
                "WHERE id = $1::uuid",
                task_ids[2], datetime(2026, 10, 3, tzinfo=UTC),
            )
        finally:
            await migrator.close()

        headers = {"Authorization": f"Bearer {_token()}"}
        first_page = await client.get(
            "/api/v1/tasks?status=queued&limit=1&created_after=2026-10-01T00:00:00Z",
            headers=headers,
        )
        second_page = await client.get(
            "/api/v1/tasks?status=queued&limit=1&offset=1&created_after=2026-10-01T00:00:00Z",
            headers=headers,
        )
        bounded = await client.get(
            "/api/v1/tasks?created_before=2026-10-03T00:00:00Z",
            headers=headers,
        )

    assert [row["task_id"] for row in first_page.json()["items"]] == [task_ids[2]]
    assert first_page.json()["has_more"] is True
    assert [row["task_id"] for row in second_page.json()["items"]] == [task_ids[0]]
    assert [row["task_id"] for row in bounded.json()["items"]] == [task_ids[1], task_ids[0]]


async def test_expired_idempotency_key_creates_a_new_task(migrated_db, tmp_path):
    await _seed_user_and_agent(migrated_db)
    dataset_path = tmp_path / "business.jsonl"
    write_dataset(dataset_path)
    migrator = await asyncpg.connect(migrated_db["role_dsn"]("migrator"))
    try:
        await load_dataset(migrator, dataset_path)
    finally:
        await migrator.close()

    headers = {
        "Authorization": f"Bearer {_token()}",
        "Idempotency-Key": "expired-task-key-001",
    }
    async with AsyncClient(
        transport=ASGITransport(app=_app(migrated_db)), base_url="http://testserver"
    ) as client:
        first = await client.post("/api/v1/tasks", json=_request_body(), headers=headers)
        conn = await asyncpg.connect(migrated_db["role_dsn"]("migrator"))
        try:
            await conn.execute(
                "UPDATE runtime.idempotency_records SET expires_at = now() - interval '1 second' "
                "WHERE idempotency_key = 'expired-task-key-001'"
            )
        finally:
            await conn.close()
        second = await client.post("/api/v1/tasks", json=_request_body(), headers=headers)

    assert first.status_code == second.status_code == 202
    assert first.json()["task_id"] != second.json()["task_id"]


async def test_task_detail_and_list_hide_foreign_tasks_and_filter_owner(migrated_db, tmp_path):
    await _seed_user_and_agent(migrated_db)
    dataset_path = tmp_path / "business.jsonl"
    write_dataset(dataset_path)
    migrator = await asyncpg.connect(migrated_db["role_dsn"]("migrator"))
    try:
        await load_dataset(migrator, dataset_path)
        await migrator.execute(
            "INSERT INTO identity.users (id, email, display_name, status) "
            "VALUES ('0192a3b4-c5d6-7e8f-9012-3456789abcdf', "
            "'other-task-user@example.com', 'Other User', 'active')"
        )
    finally:
        await migrator.close()

    headers = {
        "Authorization": f"Bearer {_token()}",
        "Idempotency-Key": "detail-list-001",
    }
    async with AsyncClient(
        transport=ASGITransport(app=_app(migrated_db)), base_url="http://testserver"
    ) as client:
        created = await client.post("/api/v1/tasks", json=_request_body(), headers=headers)
        task_id = created.json()["task_id"]
        detail = await client.get(f"/api/v1/tasks/{task_id}", headers=headers)
        foreign = await client.get(
            f"/api/v1/tasks/{task_id}",
            headers={"Authorization": f"Bearer {_token('0192a3b4-c5d6-7e8f-9012-3456789abcdf')}"},
        )
        listing = await client.get("/api/v1/tasks?status=queued&limit=1", headers=headers)

    assert created.status_code == 202
    assert detail.status_code == 200
    assert detail.json()["task_id"] == task_id
    assert detail.json()["status"] == "queued"
    assert detail.json()["current_attempt_no"] == 0
    assert detail.json()["progress"]["total_steps"] == 4
    assert foreign.status_code == 404
    assert foreign.json()["error"]["code"] == "TASK_NOT_FOUND"
    assert [item["task_id"] for item in listing.json()["items"]] == [task_id]
    assert listing.json()["has_more"] is False


async def test_unpublished_agent_is_rejected_as_invalid_request(migrated_db):
    conn = await asyncpg.connect(migrated_db["role_dsn"]("migrator"))
    try:
        await conn.execute(
            "INSERT INTO identity.users (id, email, display_name, status) "
            "VALUES ($1::uuid, 'agentless-task@example.com', 'Agentless User', 'active')",
            USER_ID,
        )
    finally:
        await conn.close()
    async with AsyncClient(
        transport=ASGITransport(app=_app(migrated_db)), base_url="http://testserver"
    ) as client:
        response = await client.post(
            "/api/v1/tasks",
            json=_request_body(),
            headers={
                "Authorization": f"Bearer {_token()}",
                "Idempotency-Key": "unpublished-agent-001",
            },
        )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_REQUEST"
    conn = await asyncpg.connect(migrated_db["role_dsn"]("migrator"))
    try:
        assert await conn.fetchval("SELECT count(*) FROM runtime.tasks") == 0
        assert await conn.fetchval("SELECT count(*) FROM runtime.idempotency_records") == 0
    finally:
        await conn.close()


async def test_api_role_can_only_read_snapshot_metadata_and_cannot_mutate_tasks(migrated_db):
    conn = await asyncpg.connect(migrated_db["role_dsn"]("api"))
    try:
        snapshot_id = await conn.fetchval(
            "SELECT data_snapshot_id FROM business.data_snapshots LIMIT 1"
        )
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await conn.fetchval("SELECT count(*) FROM business.orders")
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await conn.execute(
                "UPDATE runtime.tasks SET status = 'failed' WHERE false"
            )
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await conn.execute("DELETE FROM runtime.task_events WHERE false")
    finally:
        await conn.close()
    assert snapshot_id.strip() == (
        "e38d3afb175b4d130db4e1600cef8710b448bd67151770f1ca6ad6734e0ab5b8"
    )


async def test_region_denial_and_invalid_date_leave_no_task_or_key(migrated_db):
    await _seed_user_and_agent(migrated_db)
    body = _request_body(
        input={**_request_body()["input"], "region_codes": ["west"]}
    )
    async with AsyncClient(
        transport=ASGITransport(app=_app(migrated_db)), base_url="http://testserver"
    ) as client:
        denied = await client.post(
            "/api/v1/tasks",
            json=body,
            headers={
                "Authorization": f"Bearer {_token()}",
                "Idempotency-Key": "denied-task-001",
            },
        )
        invalid_date = await client.post(
            "/api/v1/tasks",
            json=_request_body(
                input={
                    **_request_body()["input"],
                    "date_range": {
                        "start": "2026-01-01T00:00:00Z",
                        "end": "2026-04-02T00:00:00Z",
                    },
                }
            ),
            headers={
                "Authorization": f"Bearer {_token()}",
                "Idempotency-Key": "invalid-date-task-001",
            },
        )
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "REGION_ACCESS_DENIED"
    assert invalid_date.status_code == 400
    assert invalid_date.json()["error"]["code"] == "INVALID_REQUEST"
    conn = await asyncpg.connect(migrated_db["role_dsn"]("migrator"))
    try:
        state = await conn.fetchrow(
            """
            SELECT (SELECT count(*) FROM runtime.tasks) AS tasks,
                   (SELECT count(*) FROM runtime.idempotency_records) AS keys,
                   (SELECT count(*) FROM audit.audit_events
                    WHERE action = 'region.authorization'
                      AND reason_code = 'REGION_ACCESS_DENIED') AS denials
            """
        )
    finally:
        await conn.close()
    assert dict(state) == {"tasks": 0, "keys": 0, "denials": 1}


async def test_concurrent_same_key_requests_create_one_task(migrated_db, tmp_path):
    await _seed_user_and_agent(migrated_db)
    dataset_path = tmp_path / "business.jsonl"
    write_dataset(dataset_path)
    migrator = await asyncpg.connect(migrated_db["role_dsn"]("migrator"))
    try:
        await load_dataset(migrator, dataset_path)
    finally:
        await migrator.close()

    app = _app(migrated_db)
    headers = {
        "Authorization": f"Bearer {_token()}",
        "Idempotency-Key": "concurrent-task-001",
    }

    async def create():
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            return await client.post("/api/v1/tasks", json=_request_body(), headers=headers)

    first, second = await asyncio.gather(create(), create())
    assert first.status_code == second.status_code == 202
    assert first.json() == second.json()
    conn = await asyncpg.connect(migrated_db["role_dsn"]("migrator"))
    try:
        counts = await conn.fetchrow(
            """
            SELECT (SELECT count(*) FROM runtime.tasks) AS tasks,
                   (SELECT count(*) FROM runtime.task_events) AS events,
                   (SELECT count(*) FROM audit.audit_events WHERE action = 'task.created') AS audits
            """
        )
    finally:
        await conn.close()
    assert dict(counts) == {"tasks": 1, "events": 1, "audits": 1}
