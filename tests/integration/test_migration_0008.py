"""Migration 0008: attempts, steps, harness invocations, evidence, and least privilege."""

from __future__ import annotations

import uuid

import asyncpg
import pytest

pytestmark = pytest.mark.integration

NEW_TABLES = {
    "runtime": {"task_attempts", "step_executions"},
    "harness": {"model_invocations", "tool_invocations", "evidences"},
}

EXPECTED_COLUMNS = {
    ("runtime", "task_attempts"): {
        "id", "task_id", "attempt_no", "status", "worker_id", "lease_token",
        "fencing_epoch", "lease_expires_at", "heartbeat_at", "checkpoint_ref",
        "started_at", "finished_at", "error_code", "error_detail",
    },
    ("runtime", "step_executions"): {
        "id", "task_id", "step_key", "graph_node_key", "input_hash", "status",
        "result_ref", "result_json", "result_hash", "checkpoint_ref",
        "owning_attempt_id", "fencing_epoch", "started_at", "completed_at",
    },
    ("harness", "model_invocations"): {
        "id", "task_id", "attempt_id", "invocation_key", "agent_version_id",
        "provider", "model_name", "context_hash", "input_tokens", "output_tokens",
        "estimated_cost", "status", "error_code", "trace_id", "started_at",
        "finished_at",
    },
    ("harness", "tool_invocations"): {
        "id", "task_id", "attempt_id", "invocation_key", "tool_version_id",
        "request_hash", "request_json", "status", "result_json", "result_hash",
        "error_code", "started_at", "finished_at", "trace_id",
    },
    ("harness", "evidences"): {
        "id", "task_id", "attempt_id", "tool_invocation_id", "metric_key",
        "metric_version", "unit", "current_value", "previous_value",
        "absolute_change", "change_rate", "query_plan_json", "result_json",
        "result_set_hash", "row_count", "data_snapshot_id", "created_at",
    },
}

TASK_ID = "0192a3b4-c5d6-7e8f-9012-3456789ab801"
ATTEMPT_ID = "0192a3b4-c5d6-7e8f-9012-3456789ab802"
STEP_ID = "0192a3b4-c5d6-7e8f-9012-3456789ab803"
EVIDENCE_ID = "0192a3b4-c5d6-7e8f-9012-3456789ab804"
TOOL_ID = "00000000-0000-7000-8000-000000000601"
SNAPSHOT_ID = "e38d3afb175b4d130db4e1600cef8710b448bd67151770f1ca6ad6734e0ab5b8"
OWNER_ID = "0192a3b4-c5d6-7e8f-9012-3456789ab805"
PROFILE_ID = "0192a3b4-c5d6-7e8f-9012-3456789ab806"
AGENT_ID = "0192a3b4-c5d6-7e8f-9012-3456789ab807"


async def _seed_task(migrated_db) -> None:
    conn = await asyncpg.connect(migrated_db["role_dsn"]("migrator"))
    try:
        await conn.execute(
            """
            INSERT INTO identity.users (id, email, display_name, status)
            VALUES ($1::uuid, 'migration-0008@example.com', 'Migration 0008', 'active')
            """,
            OWNER_ID,
        )
        await conn.execute(
            """
            INSERT INTO registry.model_profiles
                (id, profile_key, provider, model_name, endpoint_alias,
                 credential_ref, timeout_ms, max_output_tokens)
            VALUES ($1::uuid, 'migration-0008-fake', 'fake', 'fake-v1', 'fake-local',
                    'secret://agentforge/test/fake', 2500, 768)
            """,
            PROFILE_ID,
        )
        await conn.execute(
            """
            INSERT INTO registry.agent_versions
                (id, agent_key, version, name, graph_key, model_profile_id,
                 config_hash, status)
            VALUES ($1::uuid, 'migration-0008-agent', '1.0.0', 'Migration 0008',
                    'metrics-v1', $2::uuid, $3, 'draft')
            """,
            AGENT_ID,
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
            AGENT_ID,
        )
        await conn.execute(
            """
            INSERT INTO runtime.tasks
                (id, trace_id, owner_user_id, agent_version_id, task_type, status, input_json,
                 input_hash, region_scope_json, authorization_snapshot,
                 data_snapshot_id, deadline_at)
            VALUES ($1::uuid, $1::text, $2::uuid, $3::uuid, 'business_analysis', 'running',
                    '{"question":"test"}'::jsonb, $4, '["CN_EAST"]'::jsonb,
                    '[{"region_code":"CN_EAST","permission":"read"}]'::jsonb,
                    $5, now() + interval '5 minutes')
            """,
            TASK_ID,
            OWNER_ID,
            AGENT_ID,
            "b" * 64,
            SNAPSHOT_ID,
        )
    finally:
        await conn.close()


async def test_migrations_through_0010_tables_columns_and_version(migrated_db):
    conn = await asyncpg.connect(migrated_db["role_dsn"]("migrator"))
    try:
        for schema, tables in NEW_TABLES.items():
            for table in tables:
                columns = await conn.fetch(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = $1 AND table_name = $2",
                    schema,
                    table,
                )
                assert {row["column_name"] for row in columns} == EXPECTED_COLUMNS[
                    (schema, table)
                ], f"{schema}.{table}"
        version = await conn.fetchval(
            "SELECT MAX(version) FROM agentforge.schema_migrations"
        )
        assert version == 10
        privileges = await conn.fetchrow(
            """
            SELECT
                has_table_privilege('agentforge_business_ro',
                                    'business.metric_definitions', 'SELECT') AS business_table_select,
                has_any_column_privilege('agentforge_business_ro',
                                         'business.metric_definitions', 'SELECT') AS business_column_select,
                has_table_privilege('agentforge_worker',
                                    'business.metric_definitions', 'SELECT') AS worker_select,
                has_table_privilege('agentforge_api',
                                    'business.metric_definitions', 'SELECT') AS api_select,
                has_table_privilege('agentforge_business_ro',
                                    'business.metric_definitions', 'INSERT') AS business_insert,
                has_table_privilege('agentforge_business_ro',
                                    'business.metric_definitions', 'UPDATE') AS business_update,
                has_table_privilege('agentforge_business_ro',
                                    'business.metric_definitions', 'DELETE') AS business_delete
            """
        )
        assert dict(privileges) == {
            "business_table_select": False,
            "business_column_select": True,
            "worker_select": False,
            "api_select": False,
            "business_insert": False,
            "business_update": False,
            "business_delete": False,
        }
        metadata_columns = await conn.fetch(
            """
            SELECT column_name FROM information_schema.column_privileges
            WHERE grantee = 'agentforge_business_ro' AND table_schema = 'business'
              AND table_name = 'metric_definitions' AND privilege_type = 'SELECT'
            """
        )
        assert {row["column_name"] for row in metadata_columns} == {
            "metric_key", "version", "source_view", "expression_key",
            "allowed_dimensions", "allowed_filters", "aggregation", "unit",
            "time_column", "status",
        }
        seeded_tool = await conn.fetchrow(
            "SELECT tool_key, status, is_read_only FROM registry.tool_versions "
            "WHERE id = $1::uuid",
            TOOL_ID,
        )
        assert dict(seeded_tool) == {
            "tool_key": "metrics.query",
            "status": "active",
            "is_read_only": True,
        }
        fk = await conn.fetchval(
            "SELECT confrelid::regclass::text FROM pg_constraint "
            "WHERE conname = 'task_events_attempt_fk' "
            "AND conrelid = 'runtime.task_events'::regclass"
        )
        assert fk == "runtime.task_attempts"
    finally:
        await conn.close()


async def test_worker_can_claim_and_confirm_but_cannot_rewrite_history(migrated_db):
    await _seed_task(migrated_db)
    worker = await asyncpg.connect(migrated_db["role_dsn"]("worker"))
    try:
        await worker.execute(
            """
            INSERT INTO runtime.task_attempts
                (id, task_id, attempt_no, status, worker_id, lease_token,
                 fencing_epoch, lease_expires_at, heartbeat_at)
            VALUES ($1::uuid, $2::uuid, 1, 'running', 'worker-test', $3::uuid, 1,
                    now() + interval '30 seconds', now())
            """,
            ATTEMPT_ID,
            TASK_ID,
            str(uuid.uuid4()),
        )
        await worker.execute(
            """
            INSERT INTO runtime.step_executions
                (id, task_id, step_key, graph_node_key, input_hash, status,
                 owning_attempt_id, fencing_epoch)
            VALUES ($1::uuid, $2::uuid, 'plan_metrics:0', 'plan_metrics', $3,
                    'confirmed', $4::uuid, 1)
            """,
            STEP_ID,
            TASK_ID,
            "c" * 64,
            ATTEMPT_ID,
        )
        with pytest.raises(asyncpg.PostgresError, match="immutable"):
            await worker.execute(
                "UPDATE runtime.step_executions SET result_json = '{}'::jsonb "
                "WHERE id = $1::uuid",
                STEP_ID,
            )
        await worker.execute(
            """
            INSERT INTO runtime.task_events (task_id, attempt_id, task_event_seq,
                                             event_type, payload_json)
            VALUES ($1::uuid, $2::uuid, 2, 'step.completed', '{"step_key":"plan_metrics:0"}'::jsonb)
            """,
            TASK_ID,
            ATTEMPT_ID,
        )
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await worker.execute(
                "UPDATE runtime.task_events SET payload_json = '{}'::jsonb WHERE false"
            )
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await worker.execute("DELETE FROM runtime.task_events WHERE false")
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await worker.execute(
                "INSERT INTO runtime.tasks DEFAULT VALUES"
            )
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await worker.execute(
                "UPDATE runtime.tasks SET input_json = '{}'::jsonb WHERE false"
            )
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await worker.fetchval("SELECT count(*) FROM business.orders")
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await worker.execute("DELETE FROM harness.evidences WHERE false")
    finally:
        await worker.close()


async def test_api_has_read_only_access_to_worker_records(migrated_db):
    await _seed_task(migrated_db)
    migrator = await asyncpg.connect(migrated_db["role_dsn"]("migrator"))
    try:
        await migrator.execute(
            """
            INSERT INTO runtime.task_attempts
                (id, task_id, attempt_no, status, worker_id, lease_token,
                 fencing_epoch, lease_expires_at, heartbeat_at)
            VALUES ($1::uuid, $2::uuid, 1, 'running', 'worker-test', $3::uuid, 1,
                    now() + interval '30 seconds', now())
            """,
            ATTEMPT_ID,
            TASK_ID,
            str(uuid.uuid4()),
        )
    finally:
        await migrator.close()
    api = await asyncpg.connect(migrated_db["role_dsn"]("api"))
    try:
        assert await api.fetchval(
            "SELECT count(*) FROM runtime.task_attempts WHERE task_id = $1::uuid",
            TASK_ID,
        ) == 1
        assert await api.fetchval(
            "SELECT count(*) FROM runtime.step_executions WHERE task_id = $1::uuid",
            TASK_ID,
        ) == 0
        assert await api.fetchval(
            "SELECT count(*) FROM harness.evidences WHERE task_id = $1::uuid",
            TASK_ID,
        ) == 0
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await api.execute(
                "UPDATE runtime.task_attempts SET status = 'failed' WHERE false"
            )
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await api.execute(
                "INSERT INTO runtime.step_executions DEFAULT VALUES"
            )
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await api.execute(
                "INSERT INTO harness.evidences DEFAULT VALUES"
            )
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await api.execute(
                "UPDATE harness.tool_invocations SET status = 'failed' WHERE false"
            )
    finally:
        await api.close()


async def test_platform_generated_evidence_is_immutable(migrated_db):
    await _seed_task(migrated_db)
    conn = await asyncpg.connect(migrated_db["role_dsn"]("worker"))
    try:
        await conn.execute(
            """
            INSERT INTO harness.evidences
                (id, task_id, metric_key, metric_version, unit, current_value,
                 previous_value, absolute_change, change_rate, query_plan_json,
                 result_json, result_set_hash, row_count, data_snapshot_id)
            VALUES ($1::uuid, $2::uuid, 'gmv', 1, 'CNY', 57400.00, 70000.00,
                    -12600.00, -0.18000000, '{}'::jsonb, '{}'::jsonb, $3, 1, $4)
            """,
            EVIDENCE_ID,
            TASK_ID,
            "d" * 64,
            SNAPSHOT_ID,
        )
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await conn.execute(
                "UPDATE harness.evidences SET current_value = 0 WHERE id = $1::uuid",
                EVIDENCE_ID,
            )
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await conn.execute(
                "DELETE FROM harness.evidences WHERE id = $1::uuid", EVIDENCE_ID
            )
    finally:
        await conn.close()
    migrator = await asyncpg.connect(migrated_db["role_dsn"]("migrator"))
    try:
        with pytest.raises(asyncpg.PostgresError, match="immutable"):
            await migrator.execute(
                "UPDATE harness.evidences SET current_value = 0 WHERE id = $1::uuid",
                EVIDENCE_ID,
            )
    finally:
        await migrator.close()
