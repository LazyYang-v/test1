"""Repository CLI for model, tool, and Agent version registry operations."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.request
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
    forbidden = re.compile(
        r"(?:api[_-]?key|access[_-]?token|password|secret|credential)$",
        re.IGNORECASE,
    )
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


def _decode_jsonb(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


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


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self, req: urllib.request.Request, fp: Any, code: int, msg: str,
        headers: Any, newurl: str,
    ) -> None:
        return None


async def _api_request(
    method: str, path: str, payload: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Send one authenticated Registry write request to the API."""
    base_url = os.environ.get("AGENTFORGE_API_URL", "http://127.0.0.1:8000").rstrip("/")
    parsed_url = urlsplit(base_url)
    if (
        parsed_url.scheme not in {"http", "https"}
        or not parsed_url.netloc
        or parsed_url.username is not None
        or parsed_url.password is not None
        or parsed_url.query
        or parsed_url.fragment
    ):
        raise RegistryError("AGENTFORGE_API_URL must be an HTTP(S) base URL")
    token = os.environ.get("AGENTFORGE_ACCESS_TOKEN")
    if not token:
        raise RegistryError("AGENTFORGE_ACCESS_TOKEN is required for Registry writes")
    request_body = (
        None
        if payload is None
        else json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    if request_body is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        f"{base_url}{path}", data=request_body, headers=headers, method=method
    )

    def send() -> dict[str, Any]:
        try:
            opener = urllib.request.build_opener(_NoRedirectHandler)
            with opener.open(request, timeout=30) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as error:
            try:
                response_body = json.loads(error.read())
            except (json.JSONDecodeError, UnicodeDecodeError):
                response_body = {}
            error_body = response_body.get("error", {})
            code = error_body.get("code")
            message = error_body.get("message")
            if isinstance(code, str) and isinstance(message, str):
                details = error_body.get("details")
                validation_errors = (
                    details.get("errors") if isinstance(details, Mapping) else None
                )
                if isinstance(validation_errors, list):
                    messages = [item for item in validation_errors if isinstance(item, str)]
                    if messages:
                        message = f"{message}: {'; '.join(messages)}"
                raise RegistryError(f"API request failed: {code}: {message}") from None
            raise RegistryError(f"API request failed with HTTP {error.code}") from None
        except urllib.error.URLError as error:
            raise RegistryError(f"Registry API is unavailable: {error.reason}") from None

    return await asyncio.to_thread(send)


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


def _json_equal(left: Any, right: Any) -> bool:
    if type(left) in (int, float) and type(right) in (int, float):
        return left == right
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(
            _json_equal(value, right[key]) for key, value in left.items()
        )
    if isinstance(left, list):
        return len(left) == len(right) and all(
            _json_equal(value, other) for value, other in zip(left, right)
        )
    return left == right


def _policy_override_only_tightens(
    base_policy: Mapping[str, Any], policy_override: Mapping[str, Any]
) -> bool:
    if not isinstance(base_policy, Mapping) or not isinstance(policy_override, Mapping):
        return False
    for key, value in policy_override.items():
        base_value = base_policy.get(key)
        if key.startswith("max_"):
            if type(base_value) not in (int, float) or type(value) not in (int, float):
                return False
            if value > base_value:
                return False
        elif key.startswith("allowed_"):
            if not isinstance(base_value, list) or not isinstance(value, list):
                return False
            if any(
                not any(_json_equal(candidate, permitted) for permitted in base_value)
                for candidate in value
            ):
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
    result = await _api_request(
        "POST",
        "/api/v1/admin/registry/model-profiles",
        {
            "profile_key": args.profile_key,
            "provider": args.provider,
            "model_name": args.model_name,
            "endpoint_alias": args.endpoint_alias,
            "credential_ref": args.credential_ref,
            "timeout_ms": args.timeout_ms,
            "max_output_tokens": args.max_output_tokens,
            "config_json": config,
        },
    )
    _print(result)


async def _create_agent(args: argparse.Namespace) -> None:
    config = _json_object(args.config_json, "config_json")
    _reject_embedded_credentials(config)
    result = await _api_request(
        "POST",
        "/api/v1/admin/registry/agents",
        {
            "agent_key": args.agent_key,
            "version": args.version,
            "name": args.name,
            "graph_key": args.graph_key,
            "model_profile": args.model_profile,
            "config_json": config,
        },
    )
    _print(result)


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
        "max_output_tokens, config_json FROM registry.model_profiles WHERE id = $1",
        agent["model_profile_id"],
    )
    if profile is None:
        errors.append("model profile no longer exists")
    else:
        profile = dict(profile)
        profile["config_json"] = _decode_jsonb(profile["config_json"])
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
    bindings = [dict(binding) for binding in bindings]
    for binding in bindings:
        binding["policy_json"] = _decode_jsonb(binding["policy_json"])
        binding["policy_override"] = _decode_jsonb(binding["policy_override"])
        if binding["status"] != "active":
            errors.append(f"bound tool version {binding['tool_version_id']} is retired")
        tightens = _policy_override_only_tightens(
            binding["policy_json"], binding["policy_override"]
        )
        if not tightens:
            errors.append(f"bound tool version {binding['tool_version_id']} policy override loosens policy")
    return {
        "agent": dict(agent),
        "profile": profile,
        "bindings": bindings,
    }, errors


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
    version_id = str(uuid.UUID(args.version_id))
    result = await _api_request(
        "POST", f"/api/v1/admin/registry/agents/{version_id}/publish"
    )
    _print(result)


async def _retire_agent(args: argparse.Namespace) -> None:
    version_id = str(uuid.UUID(args.version_id))
    result = await _api_request(
        "POST", f"/api/v1/admin/registry/agents/{version_id}/retire"
    )
    _print(result)


async def _bind_tool(args: argparse.Namespace) -> None:
    version_id = str(uuid.UUID(args.version_id))
    tool_id = str(uuid.UUID(args.tool_id))
    override = _json_object(args.policy_override_json, "policy_override_json")
    result = await _api_request(
        "PUT",
        f"/api/v1/admin/registry/agents/{version_id}/tools/{tool_id}",
        {"policy_override": override},
    )
    _print(result)


async def _register_tool(args: argparse.Namespace) -> None:
    input_schema = _json_object(args.input_schema_json, "input_schema_json")
    output_schema = _json_object(args.output_schema_json, "output_schema_json")
    policy = _json_object(args.policy_json, "policy_json")
    result = await _api_request(
        "POST",
        "/api/v1/admin/registry/tools",
        {
            "tool_key": args.tool_key,
            "version": args.version,
            "input_schema": input_schema,
            "output_schema": output_schema,
            "policy_json": policy,
            "execution_kind": args.execution_kind,
            "timeout_ms": args.timeout_ms,
        },
    )
    _print(result)




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
    create_agent.set_defaults(handler=_create_agent)

    validate_agent = agent_subcommands.add_parser("validate")
    validate_agent.add_argument("--version-id", required=True)
    validate_agent.set_defaults(handler=_validate_command)

    publish_agent = agent_subcommands.add_parser("publish")
    publish_agent.add_argument("--version-id", required=True)
    publish_agent.set_defaults(handler=_publish_agent)

    retire_agent = agent_subcommands.add_parser("retire")
    retire_agent.add_argument("--version-id", required=True)
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
