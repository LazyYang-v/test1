"""Registry behavior through its CLI and the real PostgreSQL boundary."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import uuid

import asyncpg
import pytest
from agentforge_core.migrations import apply_migrations
from worker_support import registry_cli_context

pytestmark = pytest.mark.integration
ACTOR_ID = "00000000-0000-7000-8000-000000000404"


def _cli(migrated_db, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["AGENTFORGE_REGISTRY_DSN"] = migrated_db["role_dsn"]("registry_cli")
    is_write = bool(args) and (
        (args[0] == "model-profile" and args[1:2] == ("register",))
        or (args[0] == "tool" and args[1:2] == ("register",))
        or (
            args[0] == "agent"
            and args[1:2] in {("create",), ("bind-tool",), ("publish",), ("retire",)}
        )
    )
    if is_write:
        api_url, access_token = registry_cli_context(migrated_db)
        env["AGENTFORGE_API_URL"] = api_url
        env["AGENTFORGE_ACCESS_TOKEN"] = access_token
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


def _tool(migrated_db, policy: dict, *, tool_key: str = "policy-tool") -> dict:
    return _result(
        _cli(
            migrated_db,
            "tool",
            "register",
            "--tool-key",
            tool_key,
            "--version",
            "1",
            "--input-schema-json",
            '{"type":"object"}',
            "--output-schema-json",
            '{"type":"object"}',
            "--policy-json",
            json.dumps(policy),
            "--execution-kind",
            "internal",
            "--timeout-ms",
            "1000",
        )
    )


async def _legacy_profile(db: dict, profile_key: str) -> dict:
    profile_id = str(uuid.uuid4())
    conn = await asyncpg.connect(db["role_dsn"]("migrator"))
    try:
        await conn.execute(
            "INSERT INTO registry.model_profiles "
            "(id, profile_key, provider, model_name, endpoint_alias, credential_ref, "
            "timeout_ms, max_output_tokens, config_json) "
            "VALUES ($1::uuid, $2, 'fake', 'fake-v1', 'fake-local', "
            "'secret://agentforge/test/fake-provider', 2500, 768, '{\"temperature\":0.2}'::jsonb)",
            profile_id,
            profile_key,
        )
    finally:
        await conn.close()
    return {"id": profile_id}


async def _legacy_draft(
    db: dict, profile_id: str, *, agent_key: str = "sales-report"
) -> dict:
    agent_id = str(uuid.uuid4())
    conn = await asyncpg.connect(db["role_dsn"]("migrator"))
    try:
        await conn.execute(
            "INSERT INTO registry.agent_versions "
            "(id, agent_key, version, name, graph_key, model_profile_id, "
            "config_json, config_hash, status) "
            "VALUES ($1::uuid, $2, '1.0.0', 'Sales report', 'metrics-v1', "
            "$3::uuid, '{\"max_tool_calls\":4}'::jsonb, $4, 'draft')",
            agent_id,
            agent_key,
            profile_id,
            "a" * 64,
        )
    finally:
        await conn.close()
    return {"id": agent_id}


async def _legacy_tool(db: dict, policy: dict) -> dict:
    tool_id = str(uuid.uuid4())
    conn = await asyncpg.connect(db["role_dsn"]("migrator"))
    try:
        await conn.execute(
            "INSERT INTO registry.tool_versions "
            "(id, tool_key, version, input_schema, output_schema, policy_json, "
            "execution_kind, timeout_ms) "
            "VALUES ($1::uuid, 'policy-tool', '1', '{\"type\":\"object\"}'::jsonb, "
            "'{\"type\":\"object\"}'::jsonb, $2::jsonb, 'internal', 1000)",
            tool_id,
            json.dumps(policy),
        )
    finally:
        await conn.close()
    return {"id": tool_id}


async def _legacy_bind(db: dict, draft: dict, tool: dict, override: dict) -> None:
    conn = await asyncpg.connect(db["role_dsn"]("migrator"))
    try:
        await conn.execute(
            "INSERT INTO registry.agent_tool_bindings "
            "(agent_version_id, tool_version_id, policy_override) "
            "VALUES ($1::uuid, $2::uuid, $3::jsonb)",
            draft["id"],
            tool["id"],
            json.dumps(override),
        )
    finally:
        await conn.close()


async def _legacy_publish(db: dict, draft: dict) -> dict:
    conn = await asyncpg.connect(db["role_dsn"]("migrator"))
    try:
        await conn.execute(
            "UPDATE registry.agent_versions SET status = 'published', "
            "model_config_snapshot = '{\"provider\":\"fake\"}'::jsonb, "
            "config_hash = $2, published_at = now() WHERE id = $1::uuid",
            draft["id"],
            "b" * 64,
        )
        row = await conn.fetchrow(
            "SELECT config_hash, model_config_snapshot, published_at "
            "FROM registry.agent_versions WHERE id = $1::uuid",
            draft["id"],
        )
        result = dict(row)
        if isinstance(result["model_config_snapshot"], str):
            result["model_config_snapshot"] = json.loads(result["model_config_snapshot"])
        result["published_at"] = result["published_at"].isoformat()
        return result
    finally:
        await conn.close()


def _publish(migrated_db, version_id: str) -> dict:
    return _result(
        _cli(
            migrated_db,
            "agent",
            "publish",
            "--version-id",
            version_id,
        )
    )


async def test_registry_migration_0013_revokes_registry_cli_write_privileges(migrated_db):
    conn = await asyncpg.connect(migrated_db["role_dsn"]("migrator"))
    try:
        registry_migrations_applied = await conn.fetchval(
            "SELECT count(*) = 3 FROM agentforge.schema_migrations WHERE version IN (4, 7, 13)"
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

    assert registry_migrations_applied is True
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
        False,
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
                "SELECT has_table_privilege(current_user, $1, 'SELECT')", table
            ) is True
            assert await conn.fetchval(
                "SELECT has_table_privilege(current_user, $1, 'INSERT,UPDATE,DELETE')", table
            ) is False
    finally:
        await conn.close()


async def test_registry_helpers_are_restricted_to_trusted_trigger_execution(migrated_db):
    conn = await asyncpg.connect(migrated_db["role_dsn"]("migrator"))
    try:
        helpers = await conn.fetch(
            """
            SELECT proc.oid::regprocedure::text AS signature, proc.prosecdef, proc.proconfig,
                   NOT EXISTS (
                       SELECT FROM aclexplode(COALESCE(proc.proacl, acldefault('f', proc.proowner))) AS acl
                       WHERE acl.grantee = 0 AND acl.privilege_type = 'EXECUTE'
                   ) AS no_public_execute
            FROM pg_proc AS proc
            JOIN pg_namespace AS namespace ON namespace.oid = proc.pronamespace
            WHERE namespace.nspname = 'registry'
            """
        )
        assert helpers
        for helper in helpers:
            assert helper["prosecdef"] is True, helper["signature"]
            assert helper["proconfig"] in (
                ["search_path=pg_catalog, registry"],
                ["search_path=pg_catalog, registry, audit"],
            ), helper["signature"]
            assert helper["no_public_execute"] is True, helper["signature"]
        for role in ("registry_cli", "api", "worker", "business_ro", "eval"):
            assert await conn.fetchval(
                """
                SELECT NOT EXISTS (
                    SELECT FROM pg_proc AS proc
                    JOIN pg_namespace AS namespace ON namespace.oid = proc.pronamespace
                    WHERE namespace.nspname = 'registry'
                      AND has_function_privilege($1, proc.oid, 'EXECUTE')
                )
                """,
                f"agentforge_{role}",
            ) is True, role
    finally:
        await conn.close()


async def test_publish_freezes_the_current_model_profile_and_audits_lifecycle(migrated_db):
    profile = _profile(migrated_db, "fake-main")
    draft = _draft(migrated_db, "fake-main")

    validated = _result(_cli(migrated_db, "agent", "validate", "--version-id", draft["id"]))
    assert validated["valid"] is True

    conn = await asyncpg.connect(migrated_db["role_dsn"]("migrator"))
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
        ("registry.agent_version.created", "allowed"),
        ("agent.version.published", "allowed"),
        ("agent.version.retired", "allowed"),
    ]
    assert all(row["actor_type"] == "user" for row in events)
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
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
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

    conn = await asyncpg.connect(migrated_db["role_dsn"]("migrator"))
    try:
        with pytest.raises(asyncpg.PostgresError, match="tool version configuration is immutable"):
            await conn.execute(
                "UPDATE registry.tool_versions SET policy_json = '{}'::jsonb WHERE id = $1::uuid",
                tool["id"],
            )
    finally:
        await conn.close()

    _publish(migrated_db, draft["id"])
    conn = await asyncpg.connect(migrated_db["role_dsn"]("migrator"))
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


INVALID_POLICY_OVERRIDES = [
    pytest.param({}, {"max_calls": 4}, id="missing-max"),
    pytest.param({}, {"allowed_regions": ["east"]}, id="missing-allowed"),
    pytest.param({}, {"allowed_regions": []}, id="missing-allowed-empty"),
    pytest.param({}, {"require_approval": False}, id="missing-require"),
    pytest.param({}, {"allow_export": True}, id="missing-allow"),
    pytest.param(
        {"allowed_routes": [{"region": "east", "mode": "read"}]},
        {"allowed_routes": [{"region": "east"}]},
        id="partial-object",
    ),
    pytest.param(
        {"allowed_routes": [["east", "west"]]},
        {"allowed_routes": [["east"]]},
        id="nested-array-subset",
    ),
    pytest.param(
        {"allowed_routes": [["east", "west"]]},
        {"allowed_routes": [["west", "east"]]},
        id="nested-array-reordered",
    ),
    pytest.param({"max_calls": None}, {"max_calls": 4}, id="null-base-max"),
    pytest.param({"allowed_regions": None}, {"allowed_regions": []}, id="null-base-allowed"),
    pytest.param({"require_approval": None}, {"require_approval": True}, id="null-base-require"),
    pytest.param({"allow_export": None}, {"allow_export": False}, id="null-base-allow"),
    pytest.param({"max_calls": 5}, {"max_calls": None}, id="null-override-max"),
    pytest.param({"allowed_regions": []}, {"allowed_regions": None}, id="null-override-allowed"),
    pytest.param({"require_approval": False}, {"require_approval": None}, id="null-override-require"),
    pytest.param({"allow_export": True}, {"allow_export": None}, id="null-override-allow"),
    pytest.param({"max_calls": True}, {"max_calls": 1}, id="boolean-base-limit"),
    pytest.param({"max_calls": 5}, {"max_calls": True}, id="boolean-override-limit"),
    pytest.param({"max_calls": 5}, {"max_calls": "3"}, id="string-override-limit"),
    pytest.param({"max_calls": "5"}, {"max_calls": 3}, id="string-base-limit"),
    pytest.param({"max_calls": 5}, {"max_calls": 6}, id="larger-limit"),
    pytest.param({"allowed_values": [True]}, {"allowed_values": [1]}, id="boolean-member"),
    pytest.param({"allowed_values": [1]}, {"allowed_values": [True]}, id="numeric-member"),
    pytest.param(
        {"allowed_values": [{"enabled": True}]},
        {"allowed_values": [{"enabled": 1}]},
        id="nested-boolean-member",
    ),
    pytest.param(
        {"allowed_values": [[False]]}, {"allowed_values": [[0]]}, id="nested-numeric-member"
    ),
    pytest.param({"allowed_values": [None]}, {"allowed_values": [False]}, id="null-member"),
    pytest.param({"allowed_regions": "east"}, {"allowed_regions": []}, id="string-base-allowed"),
    pytest.param({"allowed_regions": []}, {"allowed_regions": {}}, id="object-override-allowed"),
    pytest.param({"require_approval": 1}, {"require_approval": True}, id="numeric-base-require"),
    pytest.param({"require_approval": False}, {"require_approval": 1}, id="numeric-override-require"),
    pytest.param({"require_approval": False}, {"require_approval": False}, id="require-not-enabled"),
    pytest.param({"allow_export": 0}, {"allow_export": False}, id="numeric-base-allow"),
    pytest.param({"allow_export": True}, {"allow_export": 0}, id="numeric-override-allow"),
    pytest.param({"allow_export": False}, {"allow_export": True}, id="allow-enabled"),
    pytest.param({"timeout": 5}, {"timeout": 3}, id="unknown-prefix"),
]


VALID_POLICY_OVERRIDES = [
    pytest.param({}, {}, id="empty-override"),
    pytest.param({"max_calls": 5}, {"max_calls": 3}, id="smaller-limit"),
    pytest.param({"max_calls": 5}, {"max_calls": 5.0}, id="equal-numeric-limit"),
    pytest.param({"allowed_regions": ["east", "west"]}, {"allowed_regions": ["west"]}, id="subset"),
    pytest.param({"allowed_regions": ["east"]}, {"allowed_regions": []}, id="empty-subset"),
    pytest.param({"allowed_regions": []}, {"allowed_regions": []}, id="empty-base-array"),
    pytest.param(
        {"allowed_routes": [{"region": "east", "mode": "read"}]},
        {"allowed_routes": [{"mode": "read", "region": "east"}]},
        id="exact-object",
    ),
    pytest.param(
        {"allowed_routes": [["east", "west"]]},
        {"allowed_routes": [["east", "west"]]},
        id="ordered-nested-array",
    ),
    pytest.param(
        {"allowed_values": [{"items": [None, True, 1]}]},
        {"allowed_values": [{"items": [None, True, 1.0]}]},
        id="nested-json-types",
    ),
    pytest.param({"allowed_values": [True]}, {"allowed_values": [True]}, id="boolean-member"),
    pytest.param({"allowed_values": [1]}, {"allowed_values": [1.0]}, id="equal-numeric-member"),
    pytest.param({"allowed_values": [None]}, {"allowed_values": [None]}, id="null-member"),
    pytest.param({"require_approval": False}, {"require_approval": True}, id="require-enabled"),
    pytest.param({"require_approval": True}, {"require_approval": True}, id="require-retained"),
    pytest.param({"allow_export": True}, {"allow_export": False}, id="allow-disabled"),
    pytest.param({"allow_export": True}, {"allow_export": True}, id="allow-retained"),
    pytest.param({"allow_export": False}, {"allow_export": False}, id="disallow-retained"),
]


@pytest.mark.parametrize("base_policy,override", VALID_POLICY_OVERRIDES)
def test_valid_policy_override_can_be_bound_validated_and_published(
    migrated_db, base_policy, override
):
    _profile(migrated_db, "valid-policy")
    draft = _draft(migrated_db, "valid-policy")
    tool = _tool(migrated_db, base_policy)
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
            json.dumps(override),
        )
    )
    assert binding["policy_override"] == override
    assert _result(_cli(migrated_db, "agent", "validate", "--version-id", draft["id"]))["valid"] is True
    assert _publish(migrated_db, draft["id"])["status"] == "published"


@pytest.mark.parametrize("base_policy,override", INVALID_POLICY_OVERRIDES)
@pytest.mark.parametrize("operation", ["insert", "update"])
async def test_external_binding_rejects_invalid_policy_override(
    migrated_db, base_policy, override, operation
):
    _profile(migrated_db, "invalid-policy")
    draft = _draft(migrated_db, "invalid-policy")
    tool = _tool(migrated_db, base_policy)
    conn = await asyncpg.connect(migrated_db["role_dsn"]("migrator"))
    try:
        if operation == "update":
            await conn.execute(
                "INSERT INTO registry.agent_tool_bindings (agent_version_id, tool_version_id) "
                "VALUES ($1::uuid, $2::uuid)",
                draft["id"],
                tool["id"],
            )
            statement = (
                "UPDATE registry.agent_tool_bindings SET policy_override = $3::jsonb "
                "WHERE agent_version_id = $1::uuid AND tool_version_id = $2::uuid"
            )
        else:
            statement = (
                "INSERT INTO registry.agent_tool_bindings "
                "(agent_version_id, tool_version_id, policy_override) "
                "VALUES ($1::uuid, $2::uuid, $3::jsonb)"
            )
        with pytest.raises(asyncpg.CheckViolationError, match="only tighten"):
            await conn.execute(statement, draft["id"], tool["id"], json.dumps(override))
    finally:
        await conn.close()


@pytest.mark.parametrize("base_policy,override", INVALID_POLICY_OVERRIDES)
def test_cli_binding_rejects_invalid_policy_override(migrated_db, base_policy, override):
    _profile(migrated_db, "cli-invalid-policy")
    draft = _draft(migrated_db, "cli-invalid-policy")
    tool = _tool(migrated_db, base_policy)
    rejected = _cli(
        migrated_db,
        "agent",
        "bind-tool",
        "--version-id",
        draft["id"],
        "--tool-id",
        tool["id"],
        "--policy-override-json",
        json.dumps(override),
        check=False,
    )
    assert rejected.returncode == 1
    assert "only tighten" in rejected.stderr


async def _legacy_binding(migrated_db, draft: dict, tool: dict, override: dict) -> None:
    """Reproduce a pre-repair binding without granting the CLI trigger privileges."""
    conn = await asyncpg.connect(migrated_db["superuser_dsn"])
    try:
        async with conn.transaction():
            await conn.execute(
                "ALTER TABLE registry.agent_tool_bindings DISABLE TRIGGER agent_tool_bindings_guard"
            )
            await conn.execute(
                "INSERT INTO registry.agent_tool_bindings "
                "(agent_version_id, tool_version_id, policy_override) "
                "VALUES ($1::uuid, $2::uuid, $3::jsonb)",
                draft["id"],
                tool["id"],
                json.dumps(override),
            )
            await conn.execute(
                "ALTER TABLE registry.agent_tool_bindings ENABLE TRIGGER agent_tool_bindings_guard"
            )
    finally:
        await conn.close()


@pytest.mark.parametrize("base_policy,override", INVALID_POLICY_OVERRIDES)
@pytest.mark.parametrize("command", ["validate", "publish"])
async def test_cli_rejects_legacy_invalid_policy_override(
    migrated_db, base_policy, override, command
):
    _profile(migrated_db, "legacy-policy")
    draft = _draft(migrated_db, "legacy-policy")
    tool = _tool(migrated_db, base_policy)
    await _legacy_binding(migrated_db, draft, tool, override)

    args = ["agent", command, "--version-id", draft["id"]]
    rejected = _cli(migrated_db, *args, check=False)
    assert rejected.returncode == 1
    if command == "validate":
        assert _result(rejected)["valid"] is False
        assert "loosens policy" in " ".join(_result(rejected)["errors"])
    else:
        assert "loosens policy" in rejected.stderr
    assert _result(_cli(migrated_db, "agent", "show", "--version-id", draft["id"]))["status"] == "draft"


@pytest.mark.parametrize(
    "base_policy,override",
    [
        pytest.param({}, {"max_calls": 4}, id="missing-base"),
        pytest.param(
            {"allowed_routes": [{"region": "east", "mode": "read"}]},
            {"allowed_routes": [{"region": "east"}]},
            id="partial-object",
        ),
    ],
)
async def test_external_publication_rejects_legacy_invalid_policy_binding(
    migrated_db, base_policy, override
):
    _profile(migrated_db, "legacy-publish")
    draft = _draft(migrated_db, "legacy-publish")
    tool = _tool(migrated_db, base_policy)
    await _legacy_binding(migrated_db, draft, tool, override)

    conn = await asyncpg.connect(migrated_db["role_dsn"]("migrator"))
    try:
        with pytest.raises(asyncpg.CheckViolationError, match="only tighten"):
            await conn.execute(
                "UPDATE registry.agent_versions SET status = 'published', "
                "model_config_snapshot = '{\"provider\":\"fake\"}'::jsonb, published_at = now() "
                "WHERE id = $1::uuid",
                draft["id"],
            )
    finally:
        await conn.close()
    assert _result(_cli(migrated_db, "agent", "show", "--version-id", draft["id"]))["status"] == "draft"


@pytest.mark.parametrize("status", ["draft", "published", "retired"])
@pytest.mark.parametrize("case", ["missing-base", "partial-object"])
async def test_policy_migration_rejects_existing_invalid_bindings_without_changing_them(
    migrated_db, unmigrated_db, tmp_path, status, case
):
    versions = migrated_db["repo_root"] / "migrations" / "versions"
    for source in versions.glob("*.sql"):
        if int(source.name[:4]) < 7:
            shutil.copyfile(source, tmp_path / source.name)
    dsn = unmigrated_db["role_dsn"]("migrator")
    await apply_migrations(dsn, tmp_path, migrated_db["migrate_env"])
    legacy_db = {**migrated_db, **unmigrated_db}
    profile = await _legacy_profile(legacy_db, "upgrade-profile")
    draft = await _legacy_draft(legacy_db, profile["id"])
    if case == "missing-base":
        tool = await _legacy_tool(legacy_db, {})
        override = {"max_calls": 4}
    else:
        tool = await _legacy_tool(
            legacy_db, {"allowed_routes": [{"region": "east", "mode": "read"}]}
        )
        override = {"allowed_routes": [{"region": "east"}]}

    conn = await asyncpg.connect(legacy_db["role_dsn"]("registry_cli"))
    try:
        await conn.execute(
            "INSERT INTO registry.agent_tool_bindings "
            "(agent_version_id, tool_version_id, policy_override) "
            "VALUES ($1::uuid, $2::uuid, $3::jsonb)",
            draft["id"],
            tool["id"],
            json.dumps(override),
        )
        if status != "draft":
            await conn.execute(
                "UPDATE registry.agent_versions SET status = 'published', "
                "model_config_snapshot = '{\"provider\":\"fake\"}'::jsonb, published_at = now() "
                "WHERE id = $1::uuid",
                draft["id"],
            )
        if status == "retired":
            await conn.execute(
                "UPDATE registry.agent_versions SET status = 'retired' WHERE id = $1::uuid",
                draft["id"],
            )
        before = await conn.fetchrow(
            "SELECT row_to_json(agent)::text AS agent, row_to_json(binding)::text AS binding "
            "FROM registry.agent_versions AS agent "
            "JOIN registry.agent_tool_bindings AS binding ON binding.agent_version_id = agent.id "
            "WHERE agent.id = $1::uuid",
            draft["id"],
        )
    finally:
        await conn.close()

    migration = versions / "0007_registry_policy_validation.sql"
    shutil.copyfile(migration, tmp_path / migration.name)
    with pytest.raises(asyncpg.CheckViolationError, match="existing invalid Registry policy override") as error:
        await apply_migrations(dsn, tmp_path, migrated_db["migrate_env"])
    assert draft["id"] in str(error.value)
    assert tool["id"] in str(error.value)
    assert status in str(error.value)

    conn = await asyncpg.connect(dsn)
    try:
        assert await conn.fetchval(
            "SELECT EXISTS (SELECT FROM agentforge.schema_migrations WHERE version = 7)"
        ) is False
        after = await conn.fetchrow(
            "SELECT row_to_json(agent)::text AS agent, row_to_json(binding)::text AS binding "
            "FROM registry.agent_versions AS agent "
            "JOIN registry.agent_tool_bindings AS binding ON binding.agent_version_id = agent.id "
            "WHERE agent.id = $1::uuid",
            draft["id"],
        )
        assert after == before
    finally:
        await conn.close()


async def test_policy_migration_preserves_valid_published_bindings(
    migrated_db, unmigrated_db, tmp_path
):
    versions = migrated_db["repo_root"] / "migrations" / "versions"
    for source in versions.glob("*.sql"):
        if int(source.name[:4]) < 7:
            shutil.copyfile(source, tmp_path / source.name)
    dsn = unmigrated_db["role_dsn"]("migrator")
    await apply_migrations(dsn, tmp_path, migrated_db["migrate_env"])
    legacy_db = {**migrated_db, **unmigrated_db}
    profile = await _legacy_profile(legacy_db, "valid-upgrade")
    draft = await _legacy_draft(legacy_db, profile["id"])
    tool = await _legacy_tool(
        legacy_db,
        {"max_calls": 5, "allowed_routes": [{"region": "east", "mode": "read"}]},
    )
    override = {
        "max_calls": 3,
        "allowed_routes": [{"region": "east", "mode": "read"}],
    }
    await _legacy_bind(legacy_db, draft, tool, override)
    before = await _legacy_publish(legacy_db, draft)
    migration = versions / "0007_registry_policy_validation.sql"
    shutil.copyfile(migration, tmp_path / migration.name)
    await apply_migrations(dsn, tmp_path, migrated_db["migrate_env"])

    after = _result(_cli(legacy_db, "agent", "show", "--version-id", draft["id"]))
    assert after["status"] == "published"
    assert after["config_hash"] == before["config_hash"]
    assert after["model_config_snapshot"] == before["model_config_snapshot"]
    assert after["published_at"] == before["published_at"]
    conn = await asyncpg.connect(legacy_db["role_dsn"]("registry_cli"))
    try:
        policy = await conn.fetchval(
            "SELECT policy_override FROM registry.agent_tool_bindings "
            "WHERE agent_version_id = $1::uuid AND tool_version_id = $2::uuid",
            draft["id"],
            tool["id"],
        )
        assert json.loads(policy) == {
            "max_calls": 3,
            "allowed_routes": [{"region": "east", "mode": "read"}],
        }
    finally:
        await conn.close()


@pytest.mark.parametrize("resource", ["agent", "tool"])
async def test_external_registry_version_identifier_is_immutable(migrated_db, resource):
    if resource == "agent":
        _profile(migrated_db, "identity-profile")
        version = _draft(migrated_db, "identity-profile")
        _publish(migrated_db, version["id"])
        table = "agent_versions"
        update = "id = $2::uuid, status = 'retired'"
    else:
        version = _tool(migrated_db, {})
        table = "tool_versions"
        update = "id = $2::uuid"

    conn = await asyncpg.connect(migrated_db["role_dsn"]("migrator"))
    try:
        with pytest.raises(asyncpg.CheckViolationError, match="identifiers are immutable"):
            await conn.execute(
                f"UPDATE registry.{table} SET {update} WHERE id = $1::uuid",
                version["id"],
                str(uuid.uuid4()),
            )
        assert await conn.fetchval(
            f"SELECT id::text FROM registry.{table} WHERE id = $1::uuid", version["id"]
        ) == version["id"]
    finally:
        await conn.close()


async def test_registry_rejects_embedded_model_credentials(migrated_db):
    profile = _profile(migrated_db, "credential-guard")
    conn = await asyncpg.connect(migrated_db["role_dsn"]("migrator"))
    try:
        with pytest.raises(asyncpg.CheckViolationError, match="must use credential references"):
            await conn.execute(
                "UPDATE registry.model_profiles SET config_json = '{\"api_key\":\"raw\"}'::jsonb "
                "WHERE id = $1::uuid",
                profile["id"],
            )
    finally:
        await conn.close()
    conn = await asyncpg.connect(migrated_db["role_dsn"]("registry_cli"))
    try:
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


async def test_registry_rejects_raw_credential_reference(migrated_db):
    raw_credential = "raw-provider-token-must-not-persist"
    completed = _cli(
        migrated_db,
        "model-profile",
        "register",
        "--profile-key",
        "unsafe-credential-ref",
        "--provider",
        "fake",
        "--model-name",
        "fake-v1",
        "--endpoint-alias",
        "fake-local",
        "--credential-ref",
        raw_credential,
        "--timeout-ms",
        "1000",
        "--max-output-tokens",
        "256",
        "--config-json",
        "{}",
        check=False,
    )

    assert completed.returncode != 0
    assert "INVALID_REQUEST" in completed.stderr
    assert raw_credential not in completed.stderr


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
    dsn = migrated_db["role_dsn"]("migrator")
    blocker = await asyncpg.connect(dsn)
    binder = await asyncpg.connect(
        dsn,
        server_settings={"application_name": "registry-race-bind-after-publish"},
    )
    monitor = await asyncpg.connect(migrated_db["superuser_dsn"])
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
                "agentforge_api",
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
                "agentforge_migrator",
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
