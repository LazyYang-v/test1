"""Repository CLI for model, tool, and Agent version registry operations."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import sys
import uuid
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

import asyncpg


class RegistryError(RuntimeError):
    """Raised when a registry request is invalid or conflicts with current state."""


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _json_object(value: str, label: str) -> dict[str, Any]:
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as error:
        raise RegistryError(f"{label} must be valid JSON: {error.msg}") from None
    if not isinstance(decoded, dict):
        raise RegistryError(f"{label} must be a JSON object")
    return decoded


def _reject_embedded_credentials(value: Any, path: str = "config_json") -> None:
    forbidden = re.compile(r"(?:api[_-]?key|access[_-]?token|password|secret|credential)$", re.I)
    if isinstance(value, dict):
        for key, nested in value.items():
            if forbidden.search(key):
                raise RegistryError(
                    f"{path}.{key} looks like an embedded credential; use credential_ref"
                )
            _reject_embedded_credentials(nested, f"{path}.{key}")
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            _reject_embedded_credentials(nested, f"{path}[{index}]")


def _encode_jsonb(value: Any) -> str:
    return value if isinstance(value, str) else _canonical(value)


def _json_value(value: Any) -> Any:
    if isinstance(value, uuid.UUID):
        return str(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def _print(value: Any) -> None:
    print(json.dumps(_json_value(value), sort_keys=True, ensure_ascii=False))


async def _connect() -> asyncpg.Connection:
    dsn = os.environ.get("AGENTFORGE_REGISTRY_DSN")
    if not dsn:
        raise RegistryError("AGENTFORGE_REGISTRY_DSN is required")
    if urlsplit(dsn).username != "agentforge_registry_cli":
        raise RegistryError("registry CLI DSN must use the agentforge_registry_cli role")
    conn = await asyncpg.connect(dsn)
    await conn.set_type_codec(
        "jsonb",
        schema="pg_catalog",
        encoder=_encode_jsonb,
        decoder=json.loads,
        format="text",
    )
    return conn


def _config_hash(
    *,
    agent_key: str,
    version: str,
    graph_key: str,
    config_json: dict[str, Any],
    model_config_snapshot: dict[str, Any],
    bindings: list[dict[str, Any]],
) -> str:
    payload = {
        "agent_key": agent_key,
        "version": version,
        "graph_key": graph_key,
        "config_json": config_json,
        "model_config_snapshot": model_config_snapshot,
        "tool_bindings": bindings,
    }
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def _policy_override_only_tightens(
    base_policy: Mapping[str, Any], policy_override: Mapping[str, Any]
) -> bool:
    if not isinstance(base_policy, Mapping) or not isinstance(policy_override, Mapping):
        return False
    for key, value in policy_override.items():
        base_value = base_policy.get(key)
        if key.startswith("max_"):
            if not isinstance(base_value, (int, float)) or not isinstance(value, (int, float)):
                return False
            if value > base_value:
                return False
        elif key.startswith("allowed_"):
            if not isinstance(base_value, list) or not isinstance(value, list):
                return False
            if any(candidate not in base_value for candidate in value):
                return False
        elif key.startswith("require_"):
            if not isinstance(base_value, bool) or value is not True:
                return False
        elif key.startswith("allow_"):
            if not isinstance(base_value, bool) or not isinstance(value, bool):
                return False
            if base_value is False and value is not False:
                return False
        else:
            return False
    return True


async def _register_model_profile(args: argparse.Namespace) -> None:
    config = _json_object(args.config_json, "config_json")
    _reject_embedded_credentials(config)
    profile_id = uuid.uuid4()
    conn = await _connect()
    try:
        await conn.execute(
            """
            INSERT INTO registry.model_profiles
                (id, profile_key, provider, model_name, endpoint_alias, credential_ref,
                 timeout_ms, max_output_tokens, config_json)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb)
            """,
            profile_id,
            args.profile_key,
            args.provider,
            args.model_name,
            args.endpoint_alias,
            args.credential_ref,
            args.timeout_ms,
            args.max_output_tokens,
            _canonical(config),
        )
    except asyncpg.UniqueViolationError:
        raise RegistryError(f"model profile already exists: {args.profile_key}") from None
    finally:
        await conn.close()
    _print({"id": profile_id, "profile_key": args.profile_key})


async def _create_agent(args: argparse.Namespace) -> None:
    config = _json_object(args.config_json, "config_json")
    _reject_embedded_credentials(config)
    agent_id = uuid.uuid4()
    conn = await _connect()
    try:
        profile_id = await conn.fetchval(
            "SELECT id FROM registry.model_profiles WHERE profile_key = $1",
            args.model_profile,
        )
        if profile_id is None:
            raise RegistryError(f"model profile not found: {args.model_profile}")
        config_hash = hashlib.sha256(_canonical(config).encode("utf-8")).hexdigest()
        await conn.execute(
            """
            INSERT INTO registry.agent_versions
                (id, agent_key, version, name, graph_key, model_profile_id,
                 model_config_snapshot, config_json, config_hash, status, created_by)
            VALUES ($1, $2, $3, $4, $5, $6, '{}'::jsonb, $7::jsonb, $8, 'draft', $9)
            """,
            agent_id,
            args.agent_key,
            args.version,
            args.name,
            args.graph_key,
            profile_id,
            _canonical(config),
            config_hash,
            args.created_by,
        )
    except asyncpg.UniqueViolationError:
        raise RegistryError(
            f"agent version already exists: {args.agent_key}@{args.version}"
        ) from None
    finally:
        await conn.close()
    _print({"id": agent_id, "agent_key": args.agent_key, "version": args.version, "status": "draft"})


async def _validate_agent(
    conn: asyncpg.Connection, version_id: uuid.UUID, *, lock: bool = False
) -> tuple[dict[str, Any], list[str]]:
    lock_clause = " FOR UPDATE" if lock else ""
    agent = await conn.fetchrow(
        """
        SELECT id, agent_key, version, graph_key, model_profile_id, status
        FROM registry.agent_versions WHERE id = $1
        """ + lock_clause,
        version_id,
    )
    if agent is None:
        raise RegistryError(f"agent version not found: {version_id}")
    errors: list[str] = []
    if agent["status"] != "draft":
        errors.append("only draft agent versions can be validated for publication")
    profile = await conn.fetchrow(
        "SELECT provider, model_name, endpoint_alias, credential_ref, timeout_ms, "
        "max_output_tokens, config_json FROM registry.model_profiles WHERE id = $1 FOR SHARE",
        agent["model_profile_id"],
    )
    if profile is None:
        errors.append("model profile no longer exists")
    bindings = await conn.fetch(
        """
        SELECT b.tool_version_id, b.policy_override, t.status, t.policy_json
        FROM registry.agent_tool_bindings AS b
        JOIN registry.tool_versions AS t ON t.id = b.tool_version_id
        WHERE b.agent_version_id = $1
        ORDER BY b.tool_version_id
        """,
        version_id,
    )
    for binding in bindings:
        if binding["status"] != "active":
            errors.append(f"bound tool version {binding['tool_version_id']} is retired")
        tightens = _policy_override_only_tightens(
            binding["policy_json"], binding["policy_override"]
        )
        if not tightens:
            errors.append(f"bound tool version {binding['tool_version_id']} policy override loosens policy")
    return {"agent": dict(agent), "profile": dict(profile) if profile else None, "bindings": [dict(row) for row in bindings]}, errors


async def _validate_command(args: argparse.Namespace) -> None:
    conn = await _connect()
    try:
        _, errors = await _validate_agent(conn, uuid.UUID(args.version_id))
    finally:
        await conn.close()
    _print({"valid": not errors, "errors": errors})
    if errors:
        raise RegistryError("agent version validation failed")


async def _publish_agent(args: argparse.Namespace) -> None:
    version_id = uuid.UUID(args.version_id)
    actor_id = uuid.UUID(args.actor_id)
    conn = await _connect()
    try:
        async with conn.transaction():
            await conn.execute("SELECT set_config('agentforge.actor_id', $1, true)", str(actor_id))
            await conn.fetchval(
                "SELECT id FROM registry.agent_versions WHERE id = $1 FOR UPDATE",
                version_id,
            )
            details, errors = await _validate_agent(conn, version_id, lock=True)
            if errors:
                raise RegistryError("agent version validation failed: " + "; ".join(errors))
            profile = details["profile"]
            snapshot = {
                "provider": profile["provider"],
                "model_name": profile["model_name"],
                "endpoint_alias": profile["endpoint_alias"],
                "credential_ref": profile["credential_ref"],
                "timeout_ms": profile["timeout_ms"],
                "max_output_tokens": profile["max_output_tokens"],
                "config_json": profile["config_json"],
            }
            bindings = [
                {
                    "tool_version_id": str(row["tool_version_id"]),
                    "policy_override": row["policy_override"],
                }
                for row in details["bindings"]
            ]
            config_hash = _config_hash(
                agent_key=details["agent"]["agent_key"],
                version=details["agent"]["version"],
                graph_key=details["agent"]["graph_key"],
                config_json=(await conn.fetchval(
                    "SELECT config_json FROM registry.agent_versions WHERE id = $1",
                    version_id,
                )),
                model_config_snapshot=snapshot,
                bindings=bindings,
            )
            await conn.execute(
                """
                UPDATE registry.agent_versions
                SET model_config_snapshot = $2::jsonb,
                    config_hash = $3,
                    status = 'published',
                    published_at = now()
                WHERE id = $1
                """,
                version_id,
                _canonical(snapshot),
                config_hash,
            )
            row = await conn.fetchrow(
                "SELECT id, agent_key, version, status, model_config_snapshot, config_hash, published_at "
                "FROM registry.agent_versions WHERE id = $1",
                version_id,
            )
    finally:
        await conn.close()
    _print(dict(row))


async def _retire_agent(args: argparse.Namespace) -> None:
    version_id = uuid.UUID(args.version_id)
    actor_id = uuid.UUID(args.actor_id)
    conn = await _connect()
    try:
        async with conn.transaction():
            await conn.execute("SELECT set_config('agentforge.actor_id', $1, true)", str(actor_id))
            result = await conn.execute(
                "UPDATE registry.agent_versions SET status = 'retired' "
                "WHERE id = $1 AND status = 'published'",
                version_id,
            )
            if result == "UPDATE 0":
                raise RegistryError(f"published agent version not found: {version_id}")
            row = await conn.fetchrow(
                "SELECT id, agent_key, version, status FROM registry.agent_versions WHERE id = $1",
                version_id,
            )
    finally:
        await conn.close()
    _print(dict(row))


async def _bind_tool(args: argparse.Namespace) -> None:
    version_id = uuid.UUID(args.version_id)
    tool_id = uuid.UUID(args.tool_id)
    override = _json_object(args.policy_override_json, "policy_override_json")
    conn = await _connect()
    try:
        try:
            await conn.execute(
                """
                INSERT INTO registry.agent_tool_bindings
                    (agent_version_id, tool_version_id, policy_override)
                VALUES ($1, $2, $3::jsonb)
                """,
                version_id,
                tool_id,
                _canonical(override),
            )
        except asyncpg.CheckViolationError as error:
            raise RegistryError(str(error).split("\n", 1)[0]) from None
        row = await conn.fetchrow(
            "SELECT agent_version_id, tool_version_id, policy_override "
            "FROM registry.agent_tool_bindings WHERE agent_version_id = $1 AND tool_version_id = $2",
            version_id,
            tool_id,
        )
    finally:
        await conn.close()
    _print(dict(row))


async def _register_tool(args: argparse.Namespace) -> None:
    input_schema = _json_object(args.input_schema_json, "input_schema_json")
    output_schema = _json_object(args.output_schema_json, "output_schema_json")
    policy = _json_object(args.policy_json, "policy_json")
    tool_id = uuid.uuid4()
    conn = await _connect()
    try:
        await conn.execute(
            """
            INSERT INTO registry.tool_versions
                (id, tool_key, version, input_schema, output_schema, policy_json,
                 execution_kind, timeout_ms)
            VALUES ($1, $2, $3, $4::jsonb, $5::jsonb, $6::jsonb, $7, $8)
            """,
            tool_id,
            args.tool_key,
            args.version,
            _canonical(input_schema),
            _canonical(output_schema),
            _canonical(policy),
            args.execution_kind,
            args.timeout_ms,
        )
    except asyncpg.UniqueViolationError:
        raise RegistryError(f"tool version already exists: {args.tool_key}@{args.version}") from None
    finally:
        await conn.close()
    _print({"id": tool_id, "tool_key": args.tool_key, "version": args.version})


async def _show_agent(args: argparse.Namespace) -> None:
    conn = await _connect()
    try:
        row = await conn.fetchrow(
            "SELECT id, agent_key, version, name, graph_key, model_profile_id, "
            "model_config_snapshot, config_json, config_hash, status, published_at, created_at "
            "FROM registry.agent_versions WHERE id = $1::uuid",
            args.version_id,
        )
    finally:
        await conn.close()
    if row is None:
        raise RegistryError(f"agent version not found: {args.version_id}")
    _print(dict(row))


async def _resolve_agent(args: argparse.Namespace) -> None:
    conn = await _connect()
    try:
        row = await conn.fetchrow(
            "SELECT id, agent_key, version, name, graph_key, model_config_snapshot, "
            "config_json, config_hash, status, published_at FROM registry.agent_versions "
            "WHERE agent_key = $1 AND status = 'published' "
            "ORDER BY published_at DESC, version DESC LIMIT 1",
            args.agent_key,
        )
    finally:
        await conn.close()
    if row is None:
        raise RegistryError(f"no published agent version found for {args.agent_key}")
    _print(dict(row))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agentforge-registry")
    commands = parser.add_subparsers(dest="command", required=True)

    model_profile = commands.add_parser("model-profile")
    model_subcommands = model_profile.add_subparsers(dest="model_command", required=True)
    register_profile = model_subcommands.add_parser("register")
    register_profile.add_argument("--profile-key", required=True)
    register_profile.add_argument("--provider", required=True)
    register_profile.add_argument("--model-name", required=True)
    register_profile.add_argument("--endpoint-alias", required=True)
    register_profile.add_argument("--credential-ref", required=True)
    register_profile.add_argument("--timeout-ms", type=int, required=True)
    register_profile.add_argument("--max-output-tokens", type=int, required=True)
    register_profile.add_argument("--config-json", default="{}")
    register_profile.set_defaults(handler=_register_model_profile)

    agent = commands.add_parser("agent")
    agent_subcommands = agent.add_subparsers(dest="agent_command", required=True)
    create_agent = agent_subcommands.add_parser("create")
    create_agent.add_argument("--agent-key", required=True)
    create_agent.add_argument("--version", required=True)
    create_agent.add_argument("--name", required=True)
    create_agent.add_argument("--graph-key", required=True)
    create_agent.add_argument("--model-profile", required=True)
    create_agent.add_argument("--config-json", default="{}")
    create_agent.add_argument("--created-by", type=uuid.UUID)
    create_agent.set_defaults(handler=_create_agent)

    validate_agent = agent_subcommands.add_parser("validate")
    validate_agent.add_argument("--version-id", required=True)
    validate_agent.set_defaults(handler=_validate_command)

    publish_agent = agent_subcommands.add_parser("publish")
    publish_agent.add_argument("--version-id", required=True)
    publish_agent.add_argument("--actor-id", required=True)
    publish_agent.set_defaults(handler=_publish_agent)

    retire_agent = agent_subcommands.add_parser("retire")
    retire_agent.add_argument("--version-id", required=True)
    retire_agent.add_argument("--actor-id", required=True)
    retire_agent.set_defaults(handler=_retire_agent)

    resolve_agent = agent_subcommands.add_parser("resolve")
    resolve_agent.add_argument("--agent-key", required=True)
    resolve_agent.set_defaults(handler=_resolve_agent)

    show_agent = agent_subcommands.add_parser("show")
    show_agent.add_argument("--version-id", required=True)
    show_agent.set_defaults(handler=_show_agent)

    bind_tool = agent_subcommands.add_parser("bind-tool")
    bind_tool.add_argument("--version-id", required=True)
    bind_tool.add_argument("--tool-id", required=True)
    bind_tool.add_argument("--policy-override-json", default="{}")
    bind_tool.set_defaults(handler=_bind_tool)

    tool = commands.add_parser("tool")
    tool_subcommands = tool.add_subparsers(dest="tool_command", required=True)
    register_tool = tool_subcommands.add_parser("register")
    register_tool.add_argument("--tool-key", required=True)
    register_tool.add_argument("--version", required=True)
    register_tool.add_argument("--input-schema-json", required=True)
    register_tool.add_argument("--output-schema-json", required=True)
    register_tool.add_argument("--policy-json", default="{}")
    register_tool.add_argument("--execution-kind", choices=("internal", "mcp"), required=True)
    register_tool.add_argument("--timeout-ms", type=int, required=True)
    register_tool.set_defaults(handler=_register_tool)
    return parser


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    try:
        asyncio.run(args.handler(args))
    except (RegistryError, asyncpg.PostgresError, ValueError) as error:
        print(f"registry operation failed: {error}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
