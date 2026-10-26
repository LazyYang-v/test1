"""Registry behavior through its CLI and the real PostgreSQL boundary."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor

import asyncpg
import pytest


pytestmark = pytest.mark.integration
ACTOR_ID = "00000000-0000-7000-8000-000000000404"


def _cli(migrated_db, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["AGENTFORGE_REGISTRY_DSN"] = migrated_db["role_dsn"]("registry_cli")
    executable = migrated_db["repo_root"] / ".venv" / "bin" / "agentforge-registry"
    process = subprocess.run(
        [str(executable), *args],
        cwd=migrated_db["repo_root"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if check and process.returncode:
        raise AssertionError(process.stderr)
    return process


def _result(process: subprocess.CompletedProcess[str]) -> dict:
    return json.loads(process.stdout)


def _profile(migrated_db, profile_key: str) -> dict:
    return _result(
        _cli(
            migrated_db,
            "model-profile",
            "register",
            "--profile-key",
            profile_key,
            "--provider",
            "fake",
            "--model-name",
            "fake-v1",
            "--endpoint-alias",
            "fake-local",
            "--credential-ref",
            "secret://agentforge/test/fake-provider",
            "--timeout-ms",
            "2500",
            "--max-output-tokens",
            "768",
            "--config-json",
            '{"temperature":0.2}',
        )
    )


def _draft(migrated_db, profile_key: str, *, agent_key: str = "sales-report") -> dict:
    return _result(
        _cli(
            migrated_db,
            "agent",
            "create",
            "--agent-key",
            agent_key,
            "--version",
            "1.0.0",
            "--name",
            "Sales report",
            "--graph-key",
            "metrics-v1",
            "--model-profile",
            profile_key,
            "--config-json",
            '{"max_tool_calls":4}',
        )
    )


def _publish(migrated_db, version_id: str) -> dict:
    return _result(
        _cli(
            migrated_db,
            "agent",
            "publish",
            "--version-id",
            version_id,
            "--actor-id",
            ACTOR_ID,
        )
    )


async def test_registry_migration_is_present_in_schema_version_five(migrated_db):
    conn = await asyncpg.connect(migrated_db["role_dsn"]("migrator"))
    try:
        registry_migration_applied = await conn.fetchval(
            "SELECT EXISTS (SELECT FROM agentforge.schema_migrations WHERE version = 4)"
        )
        registry_tables = await conn.fetch(
            """
            SELECT table_name FROM information_schema.tables
            WHERE table_schema = 'registry'
            """
        )
        ownership = await conn.fetchrow(
            """
            SELECT owner.rolname AS schema_owner,
                   cli.rolsuper AS cli_superuser,
                   cli.rolcreaterole AS cli_can_create_roles,
                   api.rolsuper AS api_superuser,
                   has_schema_privilege('agentforge_registry_cli', 'registry', 'USAGE')
                       AS cli_schema_usage,
                   has_schema_privilege('agentforge_registry_cli', 'registry', 'CREATE')
                       AS cli_schema_create,
                   has_table_privilege('agentforge_registry_cli',
                       'registry.agent_versions', 'UPDATE') AS cli_table_update,
                   NOT EXISTS (
                       SELECT FROM pg_proc AS proc
                       JOIN pg_namespace AS function_schema ON function_schema.oid = proc.pronamespace
                       WHERE function_schema.nspname = 'registry'
                         AND has_function_privilege('agentforge_registry_cli', proc.oid, 'EXECUTE')
                   ) AS cli_no_registry_function_execute
            FROM pg_namespace AS n
            JOIN pg_roles AS owner ON owner.oid = n.nspowner
            JOIN pg_roles AS cli ON cli.rolname = 'agentforge_registry_cli'
            JOIN pg_roles AS api ON api.rolname = 'agentforge_api'
            WHERE n.nspname = 'registry'
            """
        )
    finally:
        await conn.close()

    assert registry_migration_applied is True
    assert {row["table_name"] for row in registry_tables} == {
        "model_profiles",
        "agent_versions",
        "tool_versions",
        "agent_tool_bindings",
    }
    assert tuple(ownership.values()) == (
        "agentforge_migrator",
        False,
        False,
        False,
        True,
        False,
        True,
        True,
    )
    conn = await asyncpg.connect(migrated_db["role_dsn"]("registry_cli"))
    try:
        assert await conn.fetchval("SELECT rolsuper FROM pg_roles WHERE rolname = current_user") is False
        assert await conn.fetchval("SELECT rolcreaterole FROM pg_roles WHERE rolname = current_user") is False
        assert await conn.fetchval(
            "SELECT pg_has_role(current_user, 'agentforge_migrator', 'SET')"
        ) is False
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await conn.execute(
                "ALTER TABLE registry.agent_versions DISABLE TRIGGER agent_versions_immutability"
            )
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await conn.execute("CREATE TABLE registry.forbidden (id INTEGER)")
        for table in (
            "registry.model_profiles",
            "registry.agent_versions",
            "registry.tool_versions",
            "registry.agent_tool_bindings",
        ):
            assert await conn.fetchval(
                "SELECT has_table_privilege(current_user, $1, 'SELECT,INSERT,UPDATE,DELETE')",
                table,
            ) is True
    finally:
        await conn.close()


async def test_publish_freezes_the_current_model_profile_and_audits_lifecycle(migrated_db):
    profile = _profile(migrated_db, "fake-main")
    draft = _draft(migrated_db, "fake-main")

    validated = _result(_cli(migrated_db, "agent", "validate", "--version-id", draft["id"]))
    assert validated["valid"] is True

    conn = await asyncpg.connect(migrated_db["role_dsn"]("registry_cli"))
    try:
        await conn.execute(
            "UPDATE registry.model_profiles SET model_name = 'fake-v2', timeout_ms = 4200 "
            "WHERE id = $1::uuid",
            profile["id"],
        )
    finally:
        await conn.close()

    published = _publish(migrated_db, draft["id"])
    snapshot = published["model_config_snapshot"]
    assert published["status"] == "published"
    assert snapshot == {
        "provider": "fake",
        "model_name": "fake-v2",
        "endpoint_alias": "fake-local",
        "credential_ref": "secret://agentforge/test/fake-provider",
        "timeout_ms": 4200,
        "max_output_tokens": 768,
        "config_json": {"temperature": 0.2},
    }
    assert "secret://" in snapshot["credential_ref"]
    assert "api_key" not in json.dumps(snapshot).lower()

    retired = _result(
        _cli(
            migrated_db,
            "agent",
            "retire",
            "--version-id",
            draft["id"],
            "--actor-id",
            ACTOR_ID,
        )
    )
    assert retired["status"] == "retired"

    resolved = _cli(migrated_db, "agent", "resolve", "--agent-key", "sales-report", check=False)
    assert resolved.returncode != 0
    assert "no published agent version" in resolved.stderr.lower()

    historical = _result(_cli(migrated_db, "agent", "show", "--version-id", draft["id"]))
    assert historical["id"] == draft["id"]
    assert historical["status"] == "retired"
    assert historical["model_config_snapshot"] == snapshot

    conn = await asyncpg.connect(migrated_db["role_dsn"]("migrator"))
    try:
        events = await conn.fetch(
            """
            SELECT actor_type, actor_id::text AS actor_id, action, resource_type,
                   resource_id, decision, occurred_at
            FROM audit.audit_events
            WHERE resource_type = 'agent_version' AND resource_id = $1
            ORDER BY id
            """,
            draft["id"],
        )
    finally:
        await conn.close()

    assert [(row["action"], row["decision"]) for row in events] == [
        ("agent.version.published", "allowed"),
        ("agent.version.retired", "allowed"),
    ]
    assert all(row["actor_type"] == "cli" for row in events)
    assert all(row["actor_id"] == ACTOR_ID for row in events)
    assert all(row["occurred_at"] is not None for row in events)


async def test_published_configuration_cannot_be_changed_by_registry_cli(migrated_db):
    _profile(migrated_db, "immutable-profile")
    draft = _draft(migrated_db, "immutable-profile")
    _publish(migrated_db, draft["id"])

    conn = await asyncpg.connect(migrated_db["role_dsn"]("registry_cli"))
    try:
        assert await conn.fetchval("SELECT rolsuper FROM pg_roles WHERE rolname = current_user") is False
        assert await conn.fetchval(
            "SELECT rolcreaterole FROM pg_roles WHERE rolname = current_user"
        ) is False
        assert await conn.fetchval(
            "SELECT pg_has_role(current_user, 'agentforge_migrator', 'SET')"
        ) is False
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await conn.execute(
                "ALTER TABLE registry.agent_versions DISABLE TRIGGER agent_versions_immutability"
            )
        attempted_updates = [
            "UPDATE registry.agent_versions SET graph_key = 'tampered' WHERE id = $1::uuid",
            "UPDATE registry.agent_versions SET config_json = '{\"tampered\":true}'::jsonb WHERE id = $1::uuid",
            "UPDATE registry.agent_versions SET model_config_snapshot = '{\"provider\":\"tampered\"}'::jsonb WHERE id = $1::uuid",
            "UPDATE registry.agent_versions SET config_hash = repeat('0', 64) WHERE id = $1::uuid",
        ]
        for statement in attempted_updates:
            with pytest.raises(asyncpg.PostgresError, match="published agent versions are immutable"):
                await conn.execute(statement, draft["id"])
    finally:
        await conn.close()


async def test_tool_policy_override_may_only_tighten_and_published_bindings_freeze(migrated_db):
    _profile(migrated_db, "policy-profile")
    draft = _draft(migrated_db, "policy-profile", agent_key="policy-agent")
    tool = _result(
        _cli(
            migrated_db,
            "tool",
            "register",
            "--tool-key",
            "gmv-query",
            "--version",
            "1",
            "--input-schema-json",
            '{"type":"object"}',
            "--output-schema-json",
            '{"type":"object"}',
            "--policy-json",
            '{"max_calls":5,"allowed_regions":["east","west"],"require_approval":false}',
            "--execution-kind",
            "internal",
            "--timeout-ms",
            "1000",
        )
    )

    loosened = _cli(
        migrated_db,
        "agent",
        "bind-tool",
        "--version-id",
        draft["id"],
        "--tool-id",
        tool["id"],
        "--policy-override-json",
        '{"max_calls":6}',
        check=False,
    )
    assert loosened.returncode != 0
    assert "only tighten" in loosened.stderr.lower()

    binding = _result(
        _cli(
            migrated_db,
            "agent",
            "bind-tool",
            "--version-id",
            draft["id"],
            "--tool-id",
            tool["id"],
            "--policy-override-json",
            '{"max_calls":3,"allowed_regions":["east"],"require_approval":true}',
        )
    )
    assert binding["policy_override"] == {
        "max_calls": 3,
        "allowed_regions": ["east"],
        "require_approval": True,
    }

    conn = await asyncpg.connect(migrated_db["role_dsn"]("registry_cli"))
    try:
        with pytest.raises(asyncpg.PostgresError, match="tool version configuration is immutable"):
            await conn.execute(
                "UPDATE registry.tool_versions SET policy_json = '{}'::jsonb WHERE id = $1::uuid",
                tool["id"],
            )
    finally:
        await conn.close()

    _publish(migrated_db, draft["id"])
    conn = await asyncpg.connect(migrated_db["role_dsn"]("registry_cli"))
    try:
        with pytest.raises(asyncpg.PostgresError, match="published agent bindings are immutable"):
            await conn.execute(
                "UPDATE registry.agent_tool_bindings SET policy_override = '{}'::jsonb "
                "WHERE agent_version_id = $1::uuid AND tool_version_id = $2::uuid",
                draft["id"],
                tool["id"],
            )
    finally:
        await conn.close()


async def test_registry_rejects_embedded_model_credentials(migrated_db):
    profile = _profile(migrated_db, "credential-guard")
    conn = await asyncpg.connect(migrated_db["role_dsn"]("registry_cli"))
    try:
        with pytest.raises(asyncpg.CheckViolationError, match="must use credential references"):
            await conn.execute(
                "UPDATE registry.model_profiles SET config_json = '{\"api_key\":\"raw\"}'::jsonb "
                "WHERE id = $1::uuid",
                profile["id"],
            )
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await conn.fetchval(
                "SELECT registry.jsonb_has_embedded_credential('{\"api_key\":\"raw\"}'::jsonb)"
            )
    finally:
        await conn.close()

    completed = _cli(
        migrated_db,
        "model-profile",
        "register",
        "--profile-key",
        "unsafe-profile",
        "--provider",
        "fake",
        "--model-name",
        "fake-v1",
        "--endpoint-alias",
        "fake-local",
        "--credential-ref",
        "secret://agentforge/test/fake-provider",
        "--timeout-ms",
        "1000",
        "--max-output-tokens",
        "256",
        "--config-json",
        '{"api_key":"must-not-persist"}',
        check=False,
    )
    assert completed.returncode != 0
    assert "credential" in completed.stderr.lower() or "secret" in completed.stderr.lower()


@pytest.mark.parametrize("first_operation", ["bind", "publish"])
async def _prepare_binding_race(migrated_db, agent_key: str) -> tuple[dict, dict]:
    _profile(migrated_db, f"{agent_key}-profile")
    draft = _draft(migrated_db, f"{agent_key}-profile", agent_key=agent_key)
    tool = _result(
        _cli(
            migrated_db,
            "tool",
            "register",
            "--tool-key",
            f"{agent_key}-tool",
            "--version",
            "1",
            "--input-schema-json",
            '{"type":"object"}',
            "--output-schema-json",
            '{"type":"object"}',
            "--policy-json",
            '{"max_calls":3}',
            "--execution-kind",
            "internal",
            "--timeout-ms",
            "1000",
        )
    )
    return draft, tool


async def _wait_for_locked_query(conn, query_fragment: str, role: str) -> None:
    deadline = asyncio.get_running_loop().time() + 10
    while asyncio.get_running_loop().time() < deadline:
        waiting = await conn.fetchval(
            """
            SELECT EXISTS (
                SELECT 1 FROM pg_stat_activity
                WHERE datname = current_database()
                  AND usename = $1
                  AND wait_event_type = 'Lock'
            )
            """,
            role,
        )
        if waiting:
            return
        await asyncio.sleep(0.01)
    activity = await conn.fetch(
        "SELECT usename, application_name, state, wait_event_type, wait_event, query "
        "FROM pg_stat_activity WHERE datname = current_database()"
    )
    raise AssertionError(
        f"database query did not wait for its row lock: {query_fragment}; "
        f"activity={ [dict(row) for row in activity] }"
    )


async def test_publish_waits_for_binding_and_freezes_it(migrated_db):
    draft, tool = await _prepare_binding_race(migrated_db, "bind-first")
    dsn = migrated_db["role_dsn"]("migrator")
    binder = await asyncpg.connect(dsn, server_settings={"application_name": "registry-race-bind-first"})
    publisher = await asyncpg.connect(dsn)
    monitor = await asyncpg.connect(dsn)
    publish_task = None
    try:
        async with binder.transaction():
            await binder.execute(
                "INSERT INTO registry.agent_tool_bindings "
                "(agent_version_id, tool_version_id, policy_override) "
                "VALUES ($1::uuid, $2::uuid, '{\"max_calls\":2}'::jsonb)",
                draft["id"],
                tool["id"],
            )
            publish_task = asyncio.create_task(
                publisher.execute(
                    "UPDATE registry.agent_versions "
                    "SET model_config_snapshot = '{\"provider\":\"fake\"}'::jsonb, "
                    "config_hash = repeat('b', 64), status = 'published', published_at = now() "
                    "WHERE id = $1::uuid",
                    draft["id"],
                )
            )
            await _wait_for_locked_query(
                monitor,
                "UPDATE registry.agent_versions SET model_config_snapshot",
                "agentforge_migrator",
            )
        await publish_task

        bindings = await monitor.fetch(
            "SELECT tool_version_id::text, policy_override FROM registry.agent_tool_bindings "
            "WHERE agent_version_id = $1::uuid",
            draft["id"],
        )
    finally:
        if publish_task is not None and not publish_task.done():
            publish_task.cancel()
        await binder.close()
        await publisher.close()
        await monitor.close()

    assert len(bindings) == 1
    assert bindings[0]["tool_version_id"] == tool["id"]
    policy_override = bindings[0]["policy_override"]
    if isinstance(policy_override, str):
        policy_override = json.loads(policy_override)
    assert policy_override == {"max_calls": 2}


async def test_binding_waits_for_publish_then_is_rejected(migrated_db):
    draft, tool = await _prepare_binding_race(migrated_db, "publish-first")
    dsn = migrated_db["role_dsn"]("registry_cli")
    blocker = await asyncpg.connect(dsn)
    binder = await asyncpg.connect(
        migrated_db["role_dsn"]("registry_cli"),
        server_settings={"application_name": "registry-race-bind-after-publish"},
    )
    monitor = await asyncpg.connect(dsn)
    publish_future = asyncio.create_task(asyncio.to_thread(_publish, migrated_db, draft["id"]))
    bind_task = None
    try:
        async with blocker.transaction():
            await blocker.fetchval(
                "SELECT id FROM registry.agent_versions WHERE id = $1::uuid FOR UPDATE",
                draft["id"],
            )
            await _wait_for_locked_query(
                monitor,
                "FROM registry.agent_versions WHERE id = $1 FOR UPDATE",
                "agentforge_registry_cli",
            )
            bind_task = asyncio.create_task(
                binder.execute(
                    "INSERT INTO registry.agent_tool_bindings "
                    "(agent_version_id, tool_version_id, policy_override) "
                    "VALUES ($1::uuid, $2::uuid, '{\"max_calls\":2}'::jsonb)",
                    draft["id"],
                    tool["id"],
                )
            )
            await _wait_for_locked_query(
                monitor,
                "INSERT INTO registry.agent_tool_bindings",
                "agentforge_registry_cli",
            )

        published = await publish_future
        with pytest.raises(asyncpg.CheckViolationError, match="published agent bindings are immutable"):
            await bind_task
    finally:
        if bind_task is not None and not bind_task.done():
            bind_task.cancel()
        if not publish_future.done():
            publish_future.cancel()
        await blocker.close()
        await binder.close()
        await monitor.close()

    assert published["status"] == "published"
