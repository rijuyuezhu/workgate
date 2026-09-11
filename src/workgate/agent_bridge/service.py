"""Shared service helpers for agent bridge tool adapters."""

import atexit
import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict
from pathlib import Path
from typing import Any

from ..config.control import ControlSettingsView
from ..config.settings import get_settings
from ..schemas.result_models.agent import (
    ActivateAgentSkillOutput,
    AgentConfigStatusOutput,
    CallAgentMcpToolOutput,
    ListAgentMcpServersOutput,
    ListAgentMcpToolsOutput,
    ListAgentSkillsOutput,
)
from ..utils.serialization import to_jsonable
from .auth import (
    manager_redaction_cursor,
    manager_redaction_maps,
    manager_redaction_maps_since,
)
from .auth_store import AgentAuthStore
from .mcp import AgentMcpClientManager
from .models import AgentCapabilityRegistry, AgentMcpServerRecord, SkillRecord
from .redaction import (
    _redact_text,
    redact_configured_value_tree,
    redact_configured_values,
    redact_mapping,
)
from .registry import build_agent_registry
from .skills import activate_skill, read_agent_skill_file
from .status import registry_config_status

type AgentMcpClientManagerFactory = Callable[[float], Any]


_shared_mcp_manager_lock = threading.RLock()
_shared_mcp_manager: AgentMcpClientManager | None = None
_shared_mcp_manager_key: tuple[str, str, float, bool] | None = None


def _shared_agent_mcp_client_manager(
    settings: ControlSettingsView,
    *,
    allow_stdio: bool = True,
) -> AgentMcpClientManager:
    """Reuse one manager per active transport policy and service configuration."""
    global _shared_mcp_manager, _shared_mcp_manager_key

    key = (
        str(Path(settings.agent_config_dir).expanduser().resolve()),
        str(Path(settings.agent_auth_dir).expanduser().resolve()),
        float(settings.agent_mcp_call_timeout_s),
        allow_stdio,
    )
    with _shared_mcp_manager_lock:
        if _shared_mcp_manager is not None and _shared_mcp_manager_key == key:
            return _shared_mcp_manager
        if _shared_mcp_manager is not None:
            _shared_mcp_manager.close()
        _shared_mcp_manager = None
        _shared_mcp_manager_key = None
        manager = AgentMcpClientManager(
            settings.agent_mcp_call_timeout_s,
            AgentAuthStore(settings.agent_auth_dir),
            allow_stdio=allow_stdio,
        )
        _shared_mcp_manager = manager
        _shared_mcp_manager_key = key
        return manager


def _close_shared_agent_mcp_client_manager() -> None:
    """Best-effort teardown for persistent stdio children at interpreter shutdown."""
    global _shared_mcp_manager, _shared_mcp_manager_key

    with _shared_mcp_manager_lock:
        manager = _shared_mcp_manager
        _shared_mcp_manager = None
        _shared_mcp_manager_key = None
    if manager is not None:
        manager.close()


atexit.register(_close_shared_agent_mcp_client_manager)


def build_network_agent_registry_from_settings(
    settings: ControlSettingsView | None = None,
    client_manager_factory: AgentMcpClientManagerFactory = AgentMcpClientManager,
) -> AgentCapabilityRegistry:
    """Build the control-owned HTTP/SSE registry without machine/Skill policy."""
    active_settings: ControlSettingsView = settings or get_settings()
    if client_manager_factory is AgentMcpClientManager:
        client_manager = _shared_agent_mcp_client_manager(
            active_settings, allow_stdio=False
        )
    else:
        client_manager = client_manager_factory(
            active_settings.agent_mcp_call_timeout_s
        )
    return build_agent_registry(
        active_settings.agent_config_dir,
        client_manager,
        active_settings.agent_mcp_probe_timeout_s,
        None if active_settings.agent_dynamic_mcp_tools else False,
        False,
        project_root=active_settings.agent_config_dir,
        max_skills=0,
        max_skill_related_files=0,
        max_skill_scan_entries=0,
        max_skill_path_bytes=0,
        max_skill_entry_bytes=0,
        include_project_skills=False,
        mcp_server_types=frozenset({"http", "sse"}),
        scan_skills=False,
    )


def tool_value(source: Any, name: str, default: Any = None) -> Any:
    """Read a tool attribute from either a mapping-style or object-style MCP representation."""
    if isinstance(source, dict):
        return source.get(name, default)
    return getattr(source, name, default)


def agent_mcp_tool_row(
    server: str,
    tool: Any,
    env: dict[str, str],
    headers: dict[str, str],
    dynamic_tool_name: str | None = None,
) -> dict[str, Any]:
    """Convert an upstream MCP tool into a redacted status row."""
    jsonable_tool = to_jsonable(tool)
    data = jsonable_tool if isinstance(jsonable_tool, Mapping) else {}

    input_schema = data.get("input_schema")
    if input_schema is None:
        input_schema = data.get("inputSchema")
    if input_schema is None:
        input_schema = tool_value(tool, "input_schema")
    if input_schema is None:
        input_schema = tool_value(tool, "inputSchema", {})

    row = {
        "server": server,
        "tool": redact_configured_value_tree(
            str(data.get("name") or tool_value(tool, "name", "")), env, headers
        ),
        "description": redact_configured_value_tree(
            str(
                data.get("description")
                or tool_value(tool, "description", "")
                or ""
            ),
            env,
            headers,
        ),
        "input_schema": redact_configured_value_tree(
            input_schema or {}, env, headers
        ),
    }
    if dynamic_tool_name is not None:
        row["dynamic_tool_name"] = redact_configured_value_tree(
            dynamic_tool_name, env, headers
        )
    return row


def redacted_mcp_call_error(
    exc: Exception, *maps: dict[str, str]
) -> ValueError:
    """Wrap upstream MCP call failures after removing configured secrets."""
    error = _redact_text(redact_configured_values(str(exc), *maps))
    return ValueError(f"Agent MCP tool call failed: {error}")


def redact_mcp_payload_strings(value: Any, *maps: dict[str, str]) -> Any:
    """Redact secret-bearing strings inside arbitrary MCP payload objects."""
    return redact_configured_value_tree(value, *maps)


def redact_mcp_result_payload(data: Any, *maps: dict[str, str]) -> Any:
    """Redact configured secrets and sensitive fields from any MCP tool result."""
    return redact_mcp_payload_strings(redact_mapping(data), *maps)


def _probe_redaction_map(record: AgentMcpServerRecord) -> dict[str, str]:
    """Expose private probe credentials only to downstream sanitizers."""
    return {
        f"probe_{index}": value
        for index, value in enumerate(record.probe_redaction_values)
    }


def agent_config_status_payload(
    registry: AgentCapabilityRegistry,
) -> AgentConfigStatusOutput:
    """Return a public, redacted agent bridge configuration status payload."""
    return AgentConfigStatusOutput(**registry_config_status(registry))


def list_agent_skills_payload(
    registry: AgentCapabilityRegistry,
) -> ListAgentSkillsOutput:
    """Return discovered agent skills, source roots, and non-fatal warnings."""
    return ListAgentSkillsOutput(
        sources=[source.public_row() for source in registry.skill_sources],
        skills=[asdict(skill) for skill in registry.skills.values()],
        warnings=registry.skill_warnings,
    )


def _skill_source(registry: AgentCapabilityRegistry, skill: SkillRecord):
    """Return the selected source record for one discovered Skill."""
    for source in registry.skill_sources:
        if (
            source.name == skill.source
            and str(source.path) == skill.source_path
        ):
            return source
    raise RuntimeError(f"Skill source is no longer available: {skill.source}")


def activate_agent_skill_payload(
    registry: AgentCapabilityRegistry,
    name: str,
    *,
    max_entry_bytes: int | None = None,
) -> ActivateAgentSkillOutput:
    """Load one discovered agent skill payload by name."""
    skill = registry.skills.get(name)
    if skill is None:
        raise ValueError(f"Unknown agent skill: {name}")
    kwargs = (
        {"max_entry_bytes": max_entry_bytes}
        if max_entry_bytes is not None
        else {}
    )

    source = _skill_source(registry, skill)
    return ActivateAgentSkillOutput(
        **activate_skill(source.config_dir, skill, **kwargs)
    )


def read_agent_skill_file_payload(
    registry: AgentCapabilityRegistry,
    name: str,
    path: str,
    *,
    max_file_bytes: int | None = None,
) -> dict[str, Any]:
    """Read one bounded related file from a discovered agent skill."""

    skill = registry.skills.get(name)
    if skill is None:
        raise ValueError(f"Unknown agent skill: {name}")
    source = _skill_source(registry, skill)
    kwargs = (
        {"max_file_bytes": max_file_bytes} if max_file_bytes is not None else {}
    )
    return read_agent_skill_file(
        source.config_dir,
        name,
        path,
        source.directory,
        source_name=source.name,
        source_path=str(source.path),
        **kwargs,
    )


def list_agent_mcp_servers_payload(
    registry: AgentCapabilityRegistry,
) -> ListAgentMcpServersOutput:
    """Return configured agent MCP server status rows."""
    return ListAgentMcpServersOutput(
        root=registry_config_status(registry)["mcp_servers"]
    )


def _agent_mcp_records(
    registry: AgentCapabilityRegistry, server: str | None
) -> Iterable[tuple[str, AgentMcpServerRecord]]:
    """Iterate MCP server records after validating an optional server filter."""
    if server is not None and server not in registry.mcp_servers:
        raise ValueError(f"Unknown agent MCP server: {server}")
    if server is not None:
        return [(server, registry.mcp_servers[server])]
    return registry.mcp_servers.items()


def list_agent_mcp_tools_payload(
    registry: AgentCapabilityRegistry, server: str | None = None
) -> ListAgentMcpToolsOutput:
    """Return redacted upstream MCP tool rows, optionally filtered by server."""
    records = _agent_mcp_records(registry, server)
    dynamic_names = {
        (record.server_name, record.tool_name): dynamic_name
        for dynamic_name, record in registry.dynamic_mcp_tool_map.items()
    }
    rows = []
    for server_name, record in records:
        env, headers = manager_redaction_maps(
            registry.client_manager, server_name, record.config
        )
        rows.extend(
            agent_mcp_tool_row(
                server_name,
                tool,
                env,
                headers,
                dynamic_names.get((server_name, raw_tool_name)),
            )
            for raw_tool_name, tool in zip(
                record.raw_tool_names, record.tools, strict=True
            )
        )
    return ListAgentMcpToolsOutput(tools=rows)


def _agent_mcp_unavailable_error(
    registry: AgentCapabilityRegistry, record: AgentMcpServerRecord
) -> str:
    """Return the redacted availability error for an unreachable MCP server."""
    if not record.error:
        return "unknown error"
    env, headers = manager_redaction_maps(
        registry.client_manager, record.name, record.config
    )
    return _redact_text(redact_configured_values(record.error, env, headers))


async def call_agent_mcp_tool_payload(
    registry: AgentCapabilityRegistry,
    server: str,
    tool: str,
    args: dict[str, Any] | None = None,
) -> CallAgentMcpToolOutput:
    """Call one upstream MCP tool and redact configured secrets from errors."""
    record = registry.mcp_servers.get(server)
    if record is None:
        raise ValueError(f"Unknown agent MCP server: {server}")
    if not record.config.enabled:
        raise ValueError(f"MCP server {server} is disabled")
    if not record.available:
        raise ValueError(
            f"MCP server {server} is unavailable: "
            f"{_agent_mcp_unavailable_error(registry, record)}"
        )
    probe_secrets = _probe_redaction_map(record)
    redaction_cursor = manager_redaction_cursor(
        registry.client_manager, server, record.config
    )
    before_env, before_headers = manager_redaction_maps(
        registry.client_manager, server, record.config
    )
    try:
        data = await registry.client_manager.call_tool(
            server, record.config, tool, args or {}
        )
    except Exception as exc:
        try:
            operation_env, operation_headers = manager_redaction_maps_since(
                registry.client_manager,
                server,
                record.config,
                redaction_cursor,
            )
        except Exception:
            raise ValueError(
                "Agent MCP tool call failed: credential redaction history unavailable"
            ) from None
        raise redacted_mcp_call_error(
            exc,
            probe_secrets,
            before_env,
            before_headers,
            operation_env,
            operation_headers,
        ) from None
    try:
        operation_env, operation_headers = manager_redaction_maps_since(
            registry.client_manager,
            server,
            record.config,
            redaction_cursor,
        )
    except Exception:
        raise ValueError(
            "Agent MCP tool call failed: credential redaction history unavailable"
        ) from None
    output = redact_mcp_result_payload(
        data,
        probe_secrets,
        before_env,
        before_headers,
        operation_env,
        operation_headers,
    )
    if not isinstance(output, dict):
        output = {"result": output}
    return CallAgentMcpToolOutput(**output)
