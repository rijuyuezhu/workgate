"""Register agent bridge skills and upstream MCP tools as callable tools on the public FastMCP server."""

import threading
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import Any

from ..tools.metadata import tool_safety_annotations
from .auth import manager_redaction_maps
from .models import AgentCapabilityRegistry
from .registry import build_agent_registry
from .service import (
    activate_agent_skill_payload,
    call_agent_mcp_tool_payload,
    redact_configured_value_tree,
    tool_value,
)
from .state import agent_registry_fingerprint

type SkillHandler = Callable[[], Awaitable[Any]]
type McpHandler = Callable[..., Awaitable[Any]]


class AgentBridgeToolReloader:
    """Keeps dynamic bridge tools synchronized with on-disk agent configuration changes."""

    def __init__(
        self,
        mcp: Any,
        registry: AgentCapabilityRegistry,
        meta: dict[str, Any],
        probe_timeout_s: float,
        dynamic_mcp_tools: bool | None,
        dynamic_skill_tools: bool | None,
        skill_limits: dict[str, int] | None = None,
    ) -> None:
        self.mcp = mcp
        self.registry = registry
        self.meta = meta
        self.probe_timeout_s = probe_timeout_s
        self.dynamic_mcp_tools = dynamic_mcp_tools
        self.dynamic_skill_tools = dynamic_skill_tools
        self.skill_limits = dict(skill_limits or {})
        self._dynamic_tool_names: set[str] = set()
        self._fingerprint = agent_registry_fingerprint(
            registry.config_dir,
            registry.skill_sources,
            self._private_state_paths(),
        )
        self._lock = threading.RLock()

    def _private_state_paths(self) -> tuple[Any, ...]:
        store = getattr(self.registry.client_manager, "auth_store", None)
        if store is None:
            return ()
        return tuple(store.fingerprint_paths())

    def current_registry(self) -> AgentCapabilityRegistry:
        """Return the latest bridge registry, refreshing dynamic tools if the config fingerprint changed."""
        self.refresh_if_needed()
        return self.registry

    def refresh_if_needed(self) -> None:
        """Rebuild the registry and dynamic tool set when bridge config files change on disk."""
        fingerprint = agent_registry_fingerprint(
            self.registry.config_dir,
            self.registry.skill_sources,
            self._private_state_paths(),
        )
        if fingerprint == self._fingerprint:
            return
        with self._lock:
            fingerprint = agent_registry_fingerprint(
                self.registry.config_dir,
                self.registry.skill_sources,
                self._private_state_paths(),
            )
            if fingerprint == self._fingerprint:
                return
            self._remove_dynamic_tools()
            self.registry = build_agent_registry(
                self.registry.config_dir,
                self.registry.client_manager,
                self.probe_timeout_s,
                self.dynamic_mcp_tools,
                self.dynamic_skill_tools,
                project_root=self.registry.project_root,
                include_project_skills=self.registry.include_project_skills,
                mcp_server_types=self.registry.mcp_server_types,
                scan_skills=self.registry.scan_skills,
                **self.skill_limits,
            )
            self._fingerprint = fingerprint
            self.register_dynamic_tools()

    def register_dynamic_tools(self) -> None:
        """Register generated skill and MCP handlers on the FastMCP server."""
        self._remove_dynamic_tools()
        for (
            dynamic_name,
            record,
        ) in self.registry.dynamic_skill_tool_map.items():
            skill = self.registry.skills[record.skill_name]
            description = f"[agent skill] Activate {record.skill_name}: {skill.description}"
            self.mcp.add_tool(
                make_skill_handler(self, record.skill_name),
                name=dynamic_name,
                description=description,
                annotations=tool_safety_annotations(
                    dynamic_name, read_only=True
                ),
                meta=self.meta,
            )
            self._dynamic_tool_names.add(dynamic_name)

        for dynamic_name, record in self.registry.dynamic_mcp_tool_map.items():
            server_record = self.registry.mcp_servers[record.server_name]
            tool = next(
                candidate
                for candidate in server_record.tools
                if str(tool_value(candidate, "name", "")) == record.tool_name
            )
            env, headers = manager_redaction_maps(
                self.registry.client_manager,
                record.server_name,
                server_record.config,
            )
            tool_description = redact_configured_value_tree(
                str(tool_value(tool, "description", "") or record.tool_name),
                env,
                headers,
            )
            description = redact_configured_value_tree(
                f"[agent mcp: {record.server_name}] {tool_description}",
                env,
                headers,
            )
            self.mcp.add_tool(
                make_mcp_handler(self, record.server_name, record.tool_name),
                name=dynamic_name,
                description=description,
                annotations=tool_safety_annotations(
                    dynamic_name, read_only=False
                ),
                meta=self.meta,
            )
            self._dynamic_tool_names.add(dynamic_name)

    def _remove_dynamic_tools(self) -> None:
        """Remove previously generated dynamic tools before rebuilding the registry."""
        for tool_name in self._dynamic_tool_names:
            with suppress(Exception):
                self.mcp.remove_tool(tool_name)
        self._dynamic_tool_names.clear()


def make_skill_handler(
    reloader: AgentBridgeToolReloader, skill_name: str
) -> SkillHandler:
    """Create a FastMCP handler that activates one discovered skill from the current registry."""

    async def handler():
        return activate_agent_skill_payload(
            reloader.current_registry(), skill_name
        )

    return handler


def make_mcp_handler(
    reloader: AgentBridgeToolReloader, server_name: str, tool_name: str
) -> McpHandler:
    """Create a FastMCP handler that proxies one upstream MCP tool with redacted arguments and errors."""

    async def handler(args: dict[str, Any] | None = None):
        return await call_agent_mcp_tool_payload(
            reloader.current_registry(), server_name, tool_name, args
        )

    return handler


def _install_agent_bridge_reload_hooks(
    mcp: Any, reloader: AgentBridgeToolReloader
) -> None:
    """Patch tool-listing paths so dynamic bridge tools refresh before discovery responses are built."""
    original_list_tools = mcp.list_tools
    original_call_tool = mcp.call_tool

    async def list_tools_with_agent_reload() -> Any:
        reloader.refresh_if_needed()
        return await original_list_tools()

    async def call_tool_with_agent_reload(
        name: str, arguments: dict[str, Any]
    ) -> Any:
        reloader.refresh_if_needed()
        return await original_call_tool(name, arguments)

    mcp.list_tools = list_tools_with_agent_reload
    mcp.call_tool = call_tool_with_agent_reload


def register_agent_bridge_dynamic_tools(
    mcp: Any,
    registry: AgentCapabilityRegistry,
    meta: dict[str, Any],
    probe_timeout_s: float = 5,
    dynamic_mcp_tools: bool | None = None,
    dynamic_skill_tools: bool | None = None,
    skill_limits: dict[str, int] | None = None,
) -> None:
    """Register dynamic bridge tools and install config-reload hooks."""
    reloader = AgentBridgeToolReloader(
        mcp,
        registry,
        meta,
        probe_timeout_s,
        dynamic_mcp_tools,
        dynamic_skill_tools,
        skill_limits,
    )
    reloader.register_dynamic_tools()
    _install_agent_bridge_reload_hooks(mcp, reloader)
