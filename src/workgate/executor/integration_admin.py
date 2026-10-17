"""The three owner-side management RPCs share one narrow Executor adapter.

Permission and CSRF are verified at the Control HTTP boundary; the executor
still refuses session-bound commands and validates every untrusted argument.
"""

import asyncio
from typing import Any

from ..agent_bridge.auth_management import manage_mcp_auth
from ..agent_bridge.management import (
    ManagedMcpConfig,
    manage_mcp_manifest,
    test_mcp_connection,
)
from ..agent_bridge.skill_management import manage_skill
from ..agent_bridge.state import load_agent_manifest
from ..config.executor import ExecutorConfig


def _optional_string(args: dict[str, Any], key: str) -> str | None:
    value = args.get(key)
    if value is not None and not isinstance(value, str):
        raise ValueError(f"Invalid {key}")
    return value


async def manage_executor_integration(
    config: ExecutorConfig, op: str, args: dict[str, Any]
) -> dict[str, Any]:
    """Mutate only the agent source owned by this Executor."""
    action = args.get("action")
    if not isinstance(action, str):
        raise ValueError("Invalid integration action")
    name = _optional_string(args, "name")
    if op == "ui.skill.manage":
        return await asyncio.to_thread(
            manage_skill,
            config.agent_config_dir,
            action,
            name=name,
            content=_optional_string(args, "content"),
            expected_revision=_optional_string(args, "expected_revision"),
            entry_only=args.get("entry_only") is True,
        )
    if op == "ui.mcp.auth":
        if not name:
            raise ValueError("MCP server name required")
        return await asyncio.to_thread(
            manage_mcp_auth,
            config.agent_config_dir,
            config.agent_auth_dir,
            action,
            name=name,
            owner_type="stdio",
            secret_name=_optional_string(args, "secret_name"),
            secret_value=_optional_string(args, "secret_value"),
        )
    if op == "ui.mcp.test":
        if not name:
            raise ValueError("MCP server name required")
        return await asyncio.to_thread(
            test_mcp_connection,
            config.agent_config_dir,
            config.agent_auth_dir,
            name,
            owner_type="stdio",
            probe_timeout_s=config.agent_mcp_probe_timeout_s,
            call_timeout_s=config.agent_mcp_call_timeout_s,
            stdio_cwd=config.default_workdir,
        )
    if op != "ui.mcp.manage":
        raise ValueError("Unknown integration operation")
    raw = args.get("config")
    if raw is not None and not isinstance(raw, dict):
        raise ValueError("Invalid MCP configuration")
    managed = ManagedMcpConfig.model_validate(raw) if raw is not None else None
    result = await asyncio.to_thread(
        manage_mcp_manifest,
        config.agent_config_dir,
        action,
        name=name,
        config=managed,
        owner_type="stdio",
        auth_dir=config.agent_auth_dir,
        expected_revision=_optional_string(args, "expected_revision"),
    )
    if action == "get" and name is not None:
        existing = load_agent_manifest(
            config.agent_config_dir
        ).data.mcp_servers[name]
        try:
            result["editable_config"] = ManagedMcpConfig.model_validate(
                existing.model_dump(mode="json", by_alias=True)
            ).model_dump(mode="json", by_alias=True, exclude_none=True)
        except ValueError:
            result["requires_replacement"] = True
    return result
