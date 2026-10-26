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


async def _seed_user_and_agent(migrated_db, *, grant_regions=("CN_EAST",)):
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
        "task_type": "business_analysis",
        "input": {
            "question": "Explain the change in CN_EAST GMV from the previous period",
            "region_codes": ["CN_EAST"],
            "date_range": {
                "start": "2025-03-31",
                "end_exclusive": "2025-04-07",
                "timezone": "UTC",
            },
            "compare_mode": "previous_period",
        },
        "execution": {},
    }
    body.update(overrides)
    return body


@pytest.fixture
async def seeded_tasks_db(migrated_db, tmp_path):
    await _seed_user_and_agent(migrated_db)
    dataset_path = tmp_path / "business.jsonl"
    write_dataset(dataset_path)
    conn = await asyncpg.connect(migrated_db["role_dsn"]("migrator"))
    try:
        await load_dataset(conn, dataset_path)
    finally:
        await conn.close()
    return migrated_db


async def _install_task_insert_barrier(conn):
    # The zz prefix places this after the production Agent publication guard.
    await conn.execute(
        """
        CREATE FUNCTION runtime.test_block_task_insert()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            PERFORM pg_advisory_xact_lock(50005);
            RETURN NEW;
        END;
        $$;
        CREATE TRIGGER zz_test_block_task_insert
        BEFORE INSERT ON runtime.tasks
        FOR EACH ROW EXECUTE FUNCTION runtime.test_block_task_insert()
        """
    )


async def _wait_for_blocked_statement(conn, statement: str, blocked_by: int) -> int:
    try:
        async with asyncio.timeout(10):
            while True:
                pid = await conn.fetchval(
                    """
                    SELECT pid FROM pg_stat_activity
                    WHERE datname = current_database() AND state = 'active'
                      AND wait_event_type = 'Lock' AND query LIKE $1
                      AND $2::integer = ANY(pg_blocking_pids(pid))
                    """,
                    f"%{statement}%", blocked_by,
                )
                if pid is not None:
                    return pid
                await asyncio.sleep(0.01)
    except TimeoutError:
        raise AssertionError(
            f"No session executing {statement!r} was blocked by session {blocked_by}"
        ) from None


async def _cancel_pending(*tasks):
    for task in tasks:
        if task is not None and not task.done():
            task.cancel()
    await asyncio.gather(*(task for task in tasks if task is not None), return_exceptions=True)


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
    assert response.headers["X-Request-ID"] == "task-create-request-001"
    payload = response.json()
    assert payload["status"] == "queued"
    task_id = payload["task_id"]
    assert payload["links"] == {
        "self": f"/api/v1/tasks/{task_id}",
        "events": f"/api/v1/tasks/{task_id}/events",
    }
    assert payload["agent_version"] == "1.0.0"
    assert datetime.fromisoformat(payload["created_at"]).tzinfo is not None

    conn = await asyncpg.connect(migrated_db["role_dsn"]("migrator"))
    try:
        task = await conn.fetchrow(
            """
            SELECT owner_user_id::text AS owner_user_id,
                   agent_version_id::text AS agent_version_id, trace_id, status,
                   input_json, region_scope_json, authorization_snapshot,
                   data_snapshot_id, current_attempt_no, created_at
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
            SELECT action, resource_id, decision, trace_id FROM audit.audit_events
            WHERE action = 'task.created' AND resource_id = $1
            """,
            task_id,
        )
    finally:
        await conn.close()

    assert task["owner_user_id"] == USER_ID
    assert task["agent_version_id"] == AGENT_VERSION_ID
    assert task["trace_id"] == task_id
    assert task["status"] == "queued"
    assert task["region_scope_json"] == '["CN_EAST"]'
    assert json.loads(task["authorization_snapshot"]) == [
        {"region_code": "CN_EAST", "permission": "read"}
    ]
    assert event["task_event_seq"] == 1
    assert event["event_type"] == "task.created"
    assert event["trace_id"] == task_id
    assert (audit["action"], audit["resource_id"], audit["decision"]) == (
        "task.created",
        task_id,
        "allowed",
    )
    assert audit["trace_id"] == task_id
    assert task["data_snapshot_id"].strip() == (
        "e38d3afb175b4d130db4e1600cef8710b448bd67151770f1ca6ad6734e0ab5b8"
    )
    assert task["current_attempt_no"] == 0
    assert task["created_at"] == datetime.fromisoformat(payload["created_at"])
    assert json.loads(task["input_json"])["compare_mode"] == "previous_period"
    assert json.loads(task["input_json"])["date_range"] == {
        "start": "2025-03-31",
        "end_exclusive": "2025-04-07",
        "timezone": "UTC",
    }


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
    await _seed_user_and_agent(migrated_db, grant_regions=("CN_EAST", "CN_NORTH"))
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
    original = _request_body()
    original.pop("execution")
    original["input"].pop("compare_mode")
    original["input"]["region_codes"] = ["CN_NORTH", "CN_EAST"]
    app = _app(migrated_db)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        first = await client.post("/api/v1/tasks", json=original, headers=headers)

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
                "region_codes": [" CN_EAST ", "CN_NORTH"],
            },
            execution={"max_attempts": 3, "timeout_seconds": 300},
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
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await conn.execute("UPDATE registry.agent_versions SET status = 'retired' WHERE false")
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await conn.execute("INSERT INTO registry.agent_versions DEFAULT VALUES")
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await conn.execute("SELECT runtime.require_published_task_agent()")
        assert await conn.fetchval(
            "SELECT has_function_privilege(current_user, "
            "'runtime.require_published_task_agent()', 'EXECUTE')"
        ) is False
    finally:
        await conn.close()
    assert snapshot_id.strip() == (
        "e38d3afb175b4d130db4e1600cef8710b448bd67151770f1ca6ad6734e0ab5b8"
    )


async def test_approved_date_only_request_preserves_iana_timezone(seeded_tasks_db):
    date_range = {
        "start": "2026-10-19",
        "end_exclusive": "2026-10-26",
        "timezone": "Asia/Shanghai",
    }
    db = seeded_tasks_db
    async with AsyncClient(
        transport=ASGITransport(app=_app(db)), base_url="http://testserver"
    ) as client:
        response = await client.post(
            "/api/v1/tasks",
            json=_request_body(input={**_request_body()["input"], "date_range": date_range}),
            headers={"Authorization": f"Bearer {_token()}", "Idempotency-Key": "approved-example"},
        )
    assert response.status_code == 202
    conn = await asyncpg.connect(db["role_dsn"]("migrator"))
    try:
        persisted_input = await conn.fetchval(
            "SELECT input_json FROM runtime.tasks WHERE id = $1::uuid",
            response.json()["task_id"],
        )
    finally:
        await conn.close()
    assert json.loads(persisted_input)["date_range"] == date_range


async def test_ninety_calendar_days_across_dst_preserves_timezone(seeded_tasks_db):
    db = seeded_tasks_db
    date_range = {
        "start": "2026-08-18",
        "end_exclusive": "2026-11-16",
        "timezone": "America/New_York",
    }
    headers = {"Authorization": f"Bearer {_token()}", "Idempotency-Key": "calendar-day-limit"}
    async with AsyncClient(
        transport=ASGITransport(app=_app(db)), base_url="http://testserver"
    ) as client:
        response = await client.post(
            "/api/v1/tasks",
            json=_request_body(input={**_request_body()["input"], "date_range": date_range}),
            headers=headers,
        )
    assert response.status_code == 202
    conn = await asyncpg.connect(db["role_dsn"]("migrator"))
    try:
        persisted_input = await conn.fetchval(
            "SELECT input_json FROM runtime.tasks WHERE id = $1::uuid",
            response.json()["task_id"],
        )
    finally:
        await conn.close()
    assert json.loads(persisted_input)["date_range"] == date_range


@pytest.mark.parametrize(
    "date_range",
    [
        {"start": "2026-10-19", "end_exclusive": "2026-10-26", "timezone": "Invalid/Timezone"},
        {"start": "2026-10-19", "end_exclusive": "2026-10-26", "timezone": "+08:00"},
        {"start": "2026-10-19", "end_exclusive": "2026-10-19", "timezone": "Asia/Shanghai"},
        {"start": "2026-10-26", "end_exclusive": "2026-10-19", "timezone": "Asia/Shanghai"},
        {"start": "2026-01-01", "end_exclusive": "2026-04-02", "timezone": "Asia/Shanghai"},
        {"start": "2026-10-19T00:00:00+08:00", "end_exclusive": "2026-10-26", "timezone": "Asia/Shanghai"},
        {"start": "2026-10-19", "end_exclusive": "2026-10-26"},
    ],
)
async def test_invalid_calendar_date_ranges_are_rejected(migrated_db, date_range):
    await _seed_user_and_agent(migrated_db)
    async with AsyncClient(
        transport=ASGITransport(app=_app(migrated_db)), base_url="http://testserver"
    ) as client:
        response = await client.post(
            "/api/v1/tasks",
            json=_request_body(input={**_request_body()["input"], "date_range": date_range}),
            headers={
                "Authorization": f"Bearer {_token()}",
                "Idempotency-Key": "invalid-calendar-range",
            },
        )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_REQUEST"


@pytest.mark.parametrize("compare_mode", [None, "", "year_over_year", "free_sql"])
async def test_unsupported_comparison_is_rejected(migrated_db, compare_mode):
    await _seed_user_and_agent(migrated_db)
    async with AsyncClient(
        transport=ASGITransport(app=_app(migrated_db)), base_url="http://testserver"
    ) as client:
        response = await client.post(
            "/api/v1/tasks",
            json=_request_body(input={**_request_body()["input"], "compare_mode": compare_mode}),
            headers={
                "Authorization": f"Bearer {_token()}",
                "Idempotency-Key": "unsupported-comparison",
            },
        )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_REQUEST"
    assert response.json()["error"]["details"]["fields"] == ["body.input.compare_mode"]


@pytest.mark.parametrize("task_type", ["analysis", "sql", "shell", "python"])
async def test_unsupported_task_type_is_rejected(migrated_db, task_type):
    await _seed_user_and_agent(migrated_db)
    async with AsyncClient(
        transport=ASGITransport(app=_app(migrated_db)), base_url="http://testserver"
    ) as client:
        response = await client.post(
            "/api/v1/tasks",
            json=_request_body(task_type=task_type),
            headers={
                "Authorization": f"Bearer {_token()}",
                "Idempotency-Key": "unsupported-task-type",
            },
        )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_REQUEST"
    assert response.json()["error"]["details"]["fields"] == ["body.task_type"]


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
                        "start": "2026-01-01",
                        "end_exclusive": "2026-04-02",
                        "timezone": "Asia/Shanghai",
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


@pytest.mark.parametrize("expired", [False, True], ids=["new-key", "expired-key"])
@pytest.mark.parametrize("conflicting", [False, True], ids=["same-hash", "different-hash"])
async def test_in_flight_idempotency_serializes_replay_conflicts_and_expiry(
    seeded_tasks_db, expired, conflicting
):
    db = seeded_tasks_db
    conn = await asyncpg.connect(db["superuser_dsn"])
    app = _app(db)
    headers = {
        "Authorization": f"Bearer {_token()}",
        "Idempotency-Key": "in-flight-create",
    }
    first = second = None
    prior_task_id = None
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        try:
            if expired:
                prior = await client.post("/api/v1/tasks", json=_request_body(), headers=headers)
                assert prior.status_code == 202
                prior_task_id = prior.json()["task_id"]
                await conn.execute(
                    "UPDATE runtime.idempotency_records "
                    "SET expires_at = now() - interval '1 second' "
                    "WHERE idempotency_key = 'in-flight-create'"
                )

            await _install_task_insert_barrier(conn)
            await conn.execute("SELECT pg_advisory_lock(50005)")
            barrier_pid = conn.get_server_pid()
            first = asyncio.create_task(client.post(
                "/api/v1/tasks", json=_request_body(), headers=headers,
            ))
            first_pid = await _wait_for_blocked_statement(
                conn, "INSERT INTO runtime.tasks", barrier_pid,
            )
            # The first request has claimed its key but cannot commit its Task.
            assert not first.done()
            second_body = _request_body()
            if conflicting:
                second_body["input"]["question"] = "A different concurrent question"
            second = asyncio.create_task(client.post(
                "/api/v1/tasks", json=second_body, headers=headers,
            ))
            await _wait_for_blocked_statement(
                conn, "INSERT INTO runtime.idempotency_records", first_pid,
            )
            assert not second.done()

            await conn.execute("SELECT pg_advisory_unlock(50005)")
            created, repeated = await asyncio.wait_for(asyncio.gather(first, second), timeout=10)
            assert created.status_code == 202
            assert created.json()["task_id"] != prior_task_id
            if conflicting:
                assert repeated.status_code == 409
                assert repeated.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
            else:
                assert repeated.status_code == 202
                assert repeated.json() == created.json()
                assert repeated.content == created.content
            counts = await conn.fetchrow(
                """
                SELECT (SELECT count(*) FROM runtime.tasks) AS tasks,
                       (SELECT count(*) FROM runtime.task_events) AS events,
                       (SELECT count(*) FROM audit.audit_events WHERE action = 'task.created') AS audits,
                       (SELECT count(*) FROM runtime.idempotency_records) AS keys
                """
            )
            expected = 2 if expired else 1
            assert dict(counts) == {"tasks": expected, "events": expected, "audits": expected, "keys": 1}
            stored = await conn.fetchrow(
                "SELECT response_status, response_json, expires_at > now() + interval '23 hours' AS retained "
                "FROM runtime.idempotency_records WHERE idempotency_key = 'in-flight-create'"
            )
            assert stored["response_status"] == 202
            assert json.loads(stored["response_json"]) == created.json()
            assert stored["retained"] is True
        finally:
            await conn.execute("SELECT pg_advisory_unlock_all()")
            await _cancel_pending(first, second)
            await conn.close()


async def test_creation_wins_retirement_lock_and_preserves_pinned_version(seeded_tasks_db):
    db = seeded_tasks_db
    observer = await asyncpg.connect(db["superuser_dsn"])
    retirement = await asyncpg.connect(db["role_dsn"]("registry_cli"))
    create = retire = None
    headers = {"Authorization": f"Bearer {_token()}", "Idempotency-Key": "create-before-retire"}
    async with AsyncClient(
        transport=ASGITransport(app=_app(db)), base_url="http://testserver"
    ) as client:
        try:
            await _install_task_insert_barrier(observer)
            await observer.execute("SELECT pg_advisory_lock(50005)")
            create = asyncio.create_task(client.post(
                "/api/v1/tasks", json=_request_body(), headers=headers,
            ))
            create_pid = await _wait_for_blocked_statement(
                observer, "INSERT INTO runtime.tasks", observer.get_server_pid(),
            )
            retire = asyncio.create_task(retirement.execute(
                "UPDATE registry.agent_versions SET status = 'retired' WHERE id = $1::uuid",
                AGENT_VERSION_ID,
            ))
            await _wait_for_blocked_statement(
                observer, "UPDATE registry.agent_versions", create_pid,
            )
            assert not retire.done()
            await observer.execute("SELECT pg_advisory_unlock(50005)")
            created, _ = await asyncio.wait_for(asyncio.gather(create, retire), timeout=10)
            assert created.status_code == 202
            assert created.json()["agent_version"] == "1.0.0"
            replay = await client.post("/api/v1/tasks", json=_request_body(), headers=headers)
            assert replay.status_code == 202
            assert replay.json() == created.json()
            detail = await client.get(created.json()["links"]["self"], headers=headers)
            assert detail.status_code == 200
            assert detail.json()["status"] == "queued"
            denied = await client.post(
                "/api/v1/tasks", json=_request_body(),
                headers={**headers, "Idempotency-Key": "new-after-retire"},
            )
            assert denied.status_code == 400
            assert denied.json()["error"]["code"] == "INVALID_REQUEST"
            pinned = await observer.fetchval(
                "SELECT agent_version_id::text FROM runtime.tasks WHERE id = $1::uuid",
                created.json()["task_id"],
            )
            assert pinned == AGENT_VERSION_ID
        finally:
            await observer.execute("SELECT pg_advisory_unlock_all()")
            await _cancel_pending(create, retire)
            await retirement.close()
            await observer.close()


async def test_retirement_wins_lock_and_creation_rechecks_publication(seeded_tasks_db):
    db = seeded_tasks_db
    observer = await asyncpg.connect(db["superuser_dsn"])
    retirement = await asyncpg.connect(db["role_dsn"]("registry_cli"))
    transaction = retirement.transaction()
    await transaction.start()
    committed = False
    create = None
    headers = {"Authorization": f"Bearer {_token()}", "Idempotency-Key": "retire-before-create"}
    async with AsyncClient(
        transport=ASGITransport(app=_app(db), raise_app_exceptions=False),
        base_url="http://testserver",
    ) as client:
        try:
            await retirement.execute(
                "UPDATE registry.agent_versions SET status = 'retired' WHERE id = $1::uuid",
                AGENT_VERSION_ID,
            )
            create = asyncio.create_task(client.post(
                "/api/v1/tasks", json=_request_body(), headers=headers,
            ))
            await _wait_for_blocked_statement(
                observer, "INSERT INTO runtime.tasks", retirement.get_server_pid(),
            )
            assert not create.done()
            await transaction.commit()
            committed = True
            response = await asyncio.wait_for(create, timeout=10)
            assert response.status_code == 400
            assert response.json()["error"]["code"] == "INVALID_REQUEST"
            counts = await observer.fetchrow(
                """
                SELECT (SELECT count(*) FROM runtime.tasks) AS tasks,
                       (SELECT count(*) FROM runtime.task_events) AS events,
                       (SELECT count(*) FROM audit.audit_events WHERE action = 'task.created') AS audits,
                       (SELECT count(*) FROM runtime.idempotency_records) AS keys
                """
            )
            assert dict(counts) == {"tasks": 0, "events": 0, "audits": 0, "keys": 0}
        finally:
            if not committed:
                await transaction.rollback()
            await _cancel_pending(create)
            await retirement.close()
            await observer.close()


async def _open_asgi_stream(app, path, headers, *, on_start=None):
    messages = []
    request_sent = False
    disconnected = asyncio.Event()
    changed = asyncio.Condition()

    async def receive():
        nonlocal request_sent
        if not request_sent:
            request_sent = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await disconnected.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        messages.append(message)
        async with changed:
            changed.notify_all()

    raw_headers = [(key.lower().encode(), value.encode()) for key, value in headers.items()]
    path_bytes = path.encode()
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path_bytes,
        "query_string": b"",
        "root_path": "",
        "headers": raw_headers,
        "client": ("testclient", 12345),
        "server": ("testserver", 80),
    }
    task = asyncio.create_task(app(scope, receive, send))

    def body():
        return b"".join(
            message.get("body", b"")
            for message in messages
            if message["type"] == "http.response.body"
        )

    async def wait_for_events(count):
        async with asyncio.timeout(10):
            async with changed:
                await changed.wait_for(
                    lambda: sum(
                        line.startswith(b"id: ") for line in body().splitlines()
                    ) >= count or task.done()
                )
        assert not task.done(), "SSE request ended before the expected events arrived"

    async def wait_for_heartbeat():
        async with asyncio.timeout(5):
            async with changed:
                await changed.wait_for(
                    lambda: b": keep-alive\n\n" in body() or task.done()
                )
        assert not task.done(), "SSE request ended before the heartbeat arrived"

    async def close():
        disconnected.set()
        await asyncio.wait_for(task, timeout=5)

    async def wait_for_response_start():
        async with asyncio.timeout(5):
            async with changed:
                await changed.wait_for(
                    lambda: any(
                        message["type"] == "http.response.start" for message in messages
                    ) or task.done()
                )
        assert not task.done(), "SSE request ended before response start"

    return {
        "messages": messages,
        "body": body,
        "wait_for_events": wait_for_events,
        "wait_for_heartbeat": wait_for_heartbeat,
        "wait_for_response_start": wait_for_response_start,
        "close": close,
        "task": task,
    }


async def _wait_for_sse_listener(db, *, different_from=None):
    conn = await asyncpg.connect(db["superuser_dsn"])
    try:
        async with asyncio.timeout(5):
            while True:
                listeners = await conn.fetch(
                    "SELECT pid FROM pg_stat_activity "
                    "WHERE application_name = 'agentforge-sse-listener' "
                    "AND datname = current_database() ORDER BY backend_start DESC"
                )
                pids = [row["pid"] for row in listeners]
                if pids and (different_from is None or different_from not in pids):
                    return pids[0]
                await asyncio.sleep(0.01)
    finally:
        await conn.close()


async def _wait_for_sse_listener_closed(db, pid):
    conn = await asyncpg.connect(db["superuser_dsn"])
    try:
        async with asyncio.timeout(5):
            while True:
                active = await conn.fetchval(
                    "SELECT EXISTS (SELECT 1 FROM pg_stat_activity "
                    "WHERE application_name = 'agentforge-sse-listener' AND pid = $1)",
                    pid,
                )
                if not active:
                    return
                await asyncio.sleep(0.01)
    finally:
        await conn.close()


async def _run_asgi_stream(app, path, headers, *, event_count):
    stream = await _open_asgi_stream(app, path, headers)
    try:
        await stream["wait_for_events"](event_count)
        response_start = next(
            message for message in stream["messages"]
            if message["type"] == "http.response.start"
        )
        return response_start["status"], dict(response_start["headers"]), stream["body"]()
    finally:
        await stream["close"]()


async def test_task_events_stream_limit_returns_retry_after(seeded_tasks_db):
    db = seeded_tasks_db
    async with AsyncClient(
        transport=ASGITransport(app=_app(db)), base_url="http://testserver"
    ) as client:
        created = await client.post(
            "/api/v1/tasks", json=_request_body(),
            headers={
                "Authorization": f"Bearer {_token()}",
                "Idempotency-Key": "task-events-limiter",
            },
        )
    task_id = created.json()["task_id"]
    app = _app(db)

    class SaturatedLimiter:
        def try_acquire(self, _principal_id):
            return False

        def release(self, _principal_id):
            raise AssertionError("a rejected stream must not release a permit")

    app.state.stream_limiter = SaturatedLimiter()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.get(
            f"/api/v1/tasks/{task_id}/events",
            headers={"Authorization": f"Bearer {_token()}"},
        )

    assert response.status_code == 429
    assert response.headers["retry-after"] == "1"
    assert response.json()["error"]["code"] == "RATE_LIMITED"


async def test_task_events_stream_requires_authentication(seeded_tasks_db):
    db = seeded_tasks_db
    missing = await AsyncClient(
        transport=ASGITransport(app=_app(db)), base_url="http://testserver"
    )
    async with missing:
        response = await missing.get(
            "/api/v1/tasks/00000000-0000-0000-0000-000000000001/events"
        )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHENTICATED"


async def test_task_events_stream_replays_persisted_history_in_order(seeded_tasks_db):
    db = seeded_tasks_db
    async with AsyncClient(
        transport=ASGITransport(app=_app(db)), base_url="http://testserver"
    ) as client:
        created = await client.post(
            "/api/v1/tasks", json=_request_body(),
            headers={
                "Authorization": f"Bearer {_token()}",
                "Idempotency-Key": "task-events-history",
            },
        )
    assert created.status_code == 202
    task_id = created.json()["task_id"]

    conn = await asyncpg.connect(db["role_dsn"]("migrator"))
    try:
        await conn.executemany(
            """
            INSERT INTO runtime.task_events
                (task_id, task_event_seq, event_type, payload_json)
            VALUES ($1::uuid, $2, $3, $4::jsonb)
            """,
            [
                (task_id, 2, "task.progress", '{"step":"plan_metrics"}'),
                (task_id, 3, "task.completed", '{"status":"completed"}'),
            ],
        )
    finally:
        await conn.close()

    status, response_headers, body = await _run_asgi_stream(
        _app(db),
        f"/api/v1/tasks/{task_id}/events",
        {"Authorization": f"Bearer {_token()}"},
        event_count=3,
    )

    assert status == 200
    assert response_headers[b"content-type"].startswith(b"text/event-stream")
    assert body.split(b"\n\n") == [
        b'id: 1\nevent: task.created\ndata: {"status":"queued"}',
        b'id: 2\nevent: task.progress\ndata: {"step":"plan_metrics"}',
        b'id: 3\nevent: task.completed\ndata: {"status":"completed"}',
        b"",
    ]


async def test_task_creation_notifies_only_committed_event(seeded_tasks_db):
    db = seeded_tasks_db
    received = asyncio.Queue()
    listener = await asyncpg.connect(
        db["role_dsn"]("api"),
        server_settings={"application_name": "test-task-event-notifier"},
    )
    await listener.add_listener(
        "agentforge_task_events",
        lambda _conn, _pid, _channel, payload: received.put_nowait(json.loads(payload)),
    )
    try:
        async with AsyncClient(
            transport=ASGITransport(app=_app(db)), base_url="http://testserver"
        ) as client:
            created = await client.post(
                "/api/v1/tasks", json=_request_body(),
                headers={
                    "Authorization": f"Bearer {_token()}",
                    "Idempotency-Key": "task-create-notification",
                },
            )
        assert created.status_code == 202
        notification = await asyncio.wait_for(received.get(), timeout=2)
        task_id = created.json()["task_id"]
        assert notification == {"seq": 1, "task_id": task_id}
        conn = await asyncpg.connect(db["role_dsn"]("migrator"))
        try:
            event = await conn.fetchrow(
                "SELECT task_event_seq, event_type FROM runtime.task_events "
                "WHERE task_id = $1::uuid AND task_event_seq = $2",
                task_id,
                notification["seq"],
            )
        finally:
            await conn.close()
        assert (event["task_event_seq"], event["event_type"]) == (1, "task.created")
    finally:
        await listener.close()


async def test_task_events_stream_resumes_strictly_after_last_event_id(seeded_tasks_db):
    db = seeded_tasks_db
    async with AsyncClient(
        transport=ASGITransport(app=_app(db)), base_url="http://testserver"
    ) as client:
        created = await client.post(
            "/api/v1/tasks", json=_request_body(),
            headers={
                "Authorization": f"Bearer {_token()}",
                "Idempotency-Key": "task-events-resume",
            },
        )
    task_id = created.json()["task_id"]
    conn = await asyncpg.connect(db["role_dsn"]("migrator"))
    try:
        await conn.executemany(
            """
            INSERT INTO runtime.task_events
                (task_id, task_event_seq, event_type, payload_json)
            VALUES ($1::uuid, $2, $3, $4::jsonb)
            """,
            [
                (task_id, 2, "step.completed", '{"step_key":"plan_metrics"}'),
                (task_id, 3, "task.completed", '{"status":"completed"}'),
            ],
        )
    finally:
        await conn.close()

    status, _, body = await _run_asgi_stream(
        _app(db),
        f"/api/v1/tasks/{task_id}/events",
        {"Authorization": f"Bearer {_token()}", "Last-Event-ID": "1"},
        event_count=2,
    )

    assert status == 200
    assert body.split(b"\n\n") == [
        b'id: 2\nevent: step.completed\ndata: {"step_key":"plan_metrics"}',
        b'id: 3\nevent: task.completed\ndata: {"status":"completed"}',
        b"",
    ]


async def test_task_events_stream_delivers_notifications_only_after_commit(seeded_tasks_db):
    db = seeded_tasks_db
    async with AsyncClient(
        transport=ASGITransport(app=_app(db)), base_url="http://testserver"
    ) as client:
        created = await client.post(
            "/api/v1/tasks", json=_request_body(),
            headers={
                "Authorization": f"Bearer {_token()}",
                "Idempotency-Key": "task-events-commit-order",
            },
        )
    task_id = created.json()["task_id"]
    app = _app(db)
    app.state.event_poll_seconds = 0.05
    stream = await _open_asgi_stream(
        app,
        f"/api/v1/tasks/{task_id}/events",
        {"Authorization": f"Bearer {_token()}", "Last-Event-ID": "1"},
    )
    await _wait_for_sse_listener(db)
    await stream["wait_for_heartbeat"]()
    received = asyncio.Queue()
    notifier = await asyncpg.connect(db["role_dsn"]("api"))
    await notifier.add_listener(
        "agentforge_task_events",
        lambda _conn, _pid, _channel, payload: received.put_nowait(json.loads(payload)),
    )
    transaction_conn = await asyncpg.connect(db["role_dsn"]("migrator"))
    transaction = transaction_conn.transaction()
    await transaction.start()
    try:
        await transaction_conn.execute(
            "INSERT INTO runtime.task_events (task_id, task_event_seq, event_type, payload_json) "
            "VALUES ($1::uuid, 2, 'step.started', '{\"step_key\":\"plan_metrics\"}'::jsonb)",
            task_id,
        )
        await transaction_conn.execute(
            "SELECT pg_notify('agentforge_task_events', $1)",
            json.dumps({"task_id": task_id, "seq": 2}),
        )
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(received.get(), timeout=0.1)
        assert b"id: 2" not in stream["body"]()
        await transaction.commit()
        notification = await asyncio.wait_for(received.get(), timeout=2)
        assert notification == {"seq": 2, "task_id": task_id}
        await stream["wait_for_events"](1)
        assert b'id: 2\nevent: step.started\ndata: {"step_key":"plan_metrics"}' in stream["body"]()
    finally:
        if transaction_conn.is_in_transaction():
            await transaction.rollback()
        await transaction_conn.close()
        await notifier.close()
        await stream["close"]()


async def test_task_events_stream_heartbeat_does_not_advance_event_id(seeded_tasks_db):
    db = seeded_tasks_db
    async with AsyncClient(
        transport=ASGITransport(app=_app(db)), base_url="http://testserver"
    ) as client:
        created = await client.post(
            "/api/v1/tasks", json=_request_body(),
            headers={
                "Authorization": f"Bearer {_token()}",
                "Idempotency-Key": "task-events-heartbeat",
            },
        )
    task_id = created.json()["task_id"]
    app = _app(db)
    app.state.event_poll_seconds = 0.05
    stream = await _open_asgi_stream(
        app,
        f"/api/v1/tasks/{task_id}/events",
        {"Authorization": f"Bearer {_token()}", "Last-Event-ID": "1"},
    )
    try:
        await stream["wait_for_heartbeat"]()
        assert stream["body"]() == b": keep-alive\n\n"
    finally:
        await stream["close"]()


async def test_task_events_stream_recovers_when_notification_is_lost(seeded_tasks_db):
    db = seeded_tasks_db
    async with AsyncClient(
        transport=ASGITransport(app=_app(db)), base_url="http://testserver"
    ) as client:
        created = await client.post(
            "/api/v1/tasks", json=_request_body(),
            headers={
                "Authorization": f"Bearer {_token()}",
                "Idempotency-Key": "task-events-lost-notify",
            },
        )
    task_id = created.json()["task_id"]
    app = _app(db)
    app.state.event_poll_seconds = 0.05
    stream = await _open_asgi_stream(
        app,
        f"/api/v1/tasks/{task_id}/events",
        {"Authorization": f"Bearer {_token()}", "Last-Event-ID": "1"},
    )
    conn = await asyncpg.connect(db["role_dsn"]("migrator"))
    try:
        await conn.execute(
            "INSERT INTO runtime.task_events (task_id, task_event_seq, event_type, payload_json) "
            "VALUES ($1::uuid, 2, 'step.completed', '{\"step_key\":\"plan_metrics\"}'::jsonb)",
            task_id,
        )
        await stream["wait_for_events"](1)
        assert b'id: 2\nevent: step.completed\ndata: {"step_key":"plan_metrics"}' in stream["body"]()
    finally:
        await conn.close()
        await stream["close"]()


async def test_task_events_stream_reconnects_after_listener_connection_closes(seeded_tasks_db):
    db = seeded_tasks_db
    async with AsyncClient(
        transport=ASGITransport(app=_app(db)), base_url="http://testserver"
    ) as client:
        created = await client.post(
            "/api/v1/tasks", json=_request_body(),
            headers={
                "Authorization": f"Bearer {_token()}",
                "Idempotency-Key": "task-events-listener-reconnect",
            },
        )
    task_id = created.json()["task_id"]
    app = _app(db)
    app.state.event_poll_seconds = 0.05
    stream = await _open_asgi_stream(
        app,
        f"/api/v1/tasks/{task_id}/events",
        {"Authorization": f"Bearer {_token()}", "Last-Event-ID": "1"},
    )
    first_pid = await _wait_for_sse_listener(db)
    conn = await asyncpg.connect(db["superuser_dsn"])
    try:
        await conn.execute("SELECT pg_terminate_backend($1)", first_pid)
    finally:
        await conn.close()

    try:
        second_pid = await _wait_for_sse_listener(db, different_from=first_pid)
        assert second_pid != first_pid
        writer = await asyncpg.connect(db["role_dsn"]("migrator"))
        try:
            await writer.execute(
                "INSERT INTO runtime.task_events (task_id, task_event_seq, event_type, payload_json) "
                "VALUES ($1::uuid, 2, 'step.completed', '{\"step_key\":\"plan_metrics\"}'::jsonb)",
                task_id,
            )
            await stream["wait_for_events"](1)
            assert b'id: 2\nevent: step.completed\ndata: {"step_key":"plan_metrics"}' in stream["body"]()
        finally:
            await writer.close()
    finally:
        await stream["close"]()


async def test_task_events_stream_closes_listener_on_disconnect(seeded_tasks_db):
    db = seeded_tasks_db
    async with AsyncClient(
        transport=ASGITransport(app=_app(db)), base_url="http://testserver"
    ) as client:
        created = await client.post(
            "/api/v1/tasks", json=_request_body(),
            headers={
                "Authorization": f"Bearer {_token()}",
                "Idempotency-Key": "task-events-listener-cleanup",
            },
        )
    task_id = created.json()["task_id"]
    stream = await _open_asgi_stream(
        _app(db),
        f"/api/v1/tasks/{task_id}/events",
        {"Authorization": f"Bearer {_token()}", "Last-Event-ID": "1"},
    )
    listener_pid = await _wait_for_sse_listener(db)
    await stream["close"]()
    await _wait_for_sse_listener_closed(db, listener_pid)


async def test_task_events_stream_hides_foreign_task_existence(seeded_tasks_db):
    db = seeded_tasks_db
    async with AsyncClient(
        transport=ASGITransport(app=_app(db)), base_url="http://testserver"
    ) as client:
        created = await client.post(
            "/api/v1/tasks", json=_request_body(),
            headers={
                "Authorization": f"Bearer {_token()}",
                "Idempotency-Key": "task-events-owner",
            },
        )
    task_id = created.json()["task_id"]
    foreign_user_id = "0192a3b4-c5d6-7e8f-9012-3456789abcdf"
    conn = await asyncpg.connect(db["role_dsn"]("migrator"))
    try:
        await conn.execute(
            "INSERT INTO identity.users (id, email, display_name, status) "
            "VALUES ($1::uuid, 'other-task-user@example.com', 'Other User', 'active')",
            foreign_user_id,
        )
    finally:
        await conn.close()

    async with AsyncClient(
        transport=ASGITransport(app=_app(db)), base_url="http://testserver"
    ) as client:
        foreign = await client.get(
            f"/api/v1/tasks/{task_id}/events",
            headers={"Authorization": f"Bearer {_token(foreign_user_id)}"},
        )
        missing = await client.get(
            "/api/v1/tasks/00000000-0000-0000-0000-000000000001/events",
            headers={"Authorization": f"Bearer {_token()}"},
        )

    assert foreign.status_code == missing.status_code == 404
    assert foreign.json()["error"]["code"] == missing.json()["error"]["code"] == "TASK_NOT_FOUND"


@pytest.mark.parametrize("cursor", ["-1", "nope", "9223372036854775808"])
async def test_task_events_stream_rejects_invalid_last_event_id(seeded_tasks_db, cursor):
    db = seeded_tasks_db
    task_id = str(uuid.uuid4())
    conn = await asyncpg.connect(db["role_dsn"]("migrator"))
    try:
        await conn.execute(
            """
            INSERT INTO runtime.tasks
                (id, owner_user_id, agent_version_id, task_type, status,
                 input_json, input_hash, region_scope_json, authorization_snapshot,
                 data_snapshot_id)
            SELECT $1::uuid, $2::uuid, id, 'business_analysis', 'queued',
                   '{}'::jsonb, repeat('a', 64), '[]'::jsonb, '[]'::jsonb,
                   (SELECT data_snapshot_id FROM business.data_snapshots LIMIT 1)
            FROM registry.agent_versions WHERE id = $3::uuid
            """,
            task_id, USER_ID, AGENT_VERSION_ID,
        )
    finally:
        await conn.close()

    async with AsyncClient(
        transport=ASGITransport(app=_app(db)), base_url="http://testserver"
    ) as client:
        response = await client.get(
            f"/api/v1/tasks/{task_id}/events",
            headers={"Authorization": f"Bearer {_token()}", "Last-Event-ID": cursor},
        )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_REQUEST"
