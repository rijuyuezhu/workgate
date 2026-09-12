"""Control-owned Agent Bridge routing across network and executor-local transports."""

from __future__ import annotations

from typing import Any

from ..agent_bridge.mcp import AgentMcpClientManager
from ..agent_bridge.models import AgentCapabilityRegistry
from ..agent_bridge.service import (
    build_network_agent_registry_from_settings,
    call_agent_mcp_tool_payload,
    list_agent_mcp_servers_payload,
    list_agent_mcp_tools_payload,
)
from ..config.control import ControlSettingsView
from ..schemas.result_models.agent import (
    CallAgentMcpToolOutput,
    ListAgentMcpServersOutput,
    ListAgentMcpToolsOutput,
)
from .sessions import ControlSessionCoordinator


class ControlAgentBridgeService:
    """Keep network MCP on control and route session-bound stdio MCP to executors."""

    def __init__(
        self, settings: ControlSettingsView, sessions: ControlSessionCoordinator
    ) -> None:
        self._settings = settings
        self._sessions = sessions

    def _network_registry(self) -> AgentCapabilityRegistry:
        return build_network_agent_registry_from_settings(
            self._settings, AgentMcpClientManager
        )

    @staticmethod
    def _require_session_id(session_id: str | None, server: str) -> str:
        if session_id is None:
            raise ValueError(
                f"Unknown agent MCP server: {server}; "
                "pass session_id for executor-local stdio MCP servers"
            )
        return session_id

    async def _resolve_server_owner(
        self, server: str, session_id: str | None
    ) -> tuple[AgentCapabilityRegistry, str]:
        registry = self._network_registry()
        control_has = server in registry.mcp_servers
        if session_id is None:
            return registry, "control" if control_has else "unknown"
        payload = await self._sessions.call_session_tool(
            "agent_mcp.list_servers", {"session_id": session_id}
        )
        executor_rows = ListAgentMcpServersOutput.model_validate(payload).root
        executor_has = server in executor_rows
        if control_has and executor_has:
            raise ValueError(
                "Agent MCP server name is ambiguous across control and the "
                f"selected executor: {server}"
            )
        if control_has:
            return registry, "control"
        if executor_has:
            return registry, "executor"
        return registry, "unknown"

    @staticmethod
    def _merge_server_rows(
        control_rows: dict[str, Any], executor_rows: dict[str, Any]
    ) -> dict[str, Any]:
        duplicates = sorted(set(control_rows) & set(executor_rows))
        if duplicates:
            names = ", ".join(duplicates)
            raise ValueError(
                "Agent MCP server names must be unique across control and the "
                f"selected executor; duplicates: {names}"
            )
        return {**control_rows, **executor_rows}

    async def list_servers(
        self, session_id: str | None = None
    ) -> ListAgentMcpServersOutput:
        control = list_agent_mcp_servers_payload(self._network_registry()).root
        if session_id is None:
            return ListAgentMcpServersOutput(root=control)
        payload = await self._sessions.call_session_tool(
            "agent_mcp.list_servers", {"session_id": session_id}
        )
        executor = ListAgentMcpServersOutput.model_validate(payload).root
        return ListAgentMcpServersOutput(
            root=self._merge_server_rows(control, executor)
        )

    async def list_tools(
        self, server: str | None = None, session_id: str | None = None
    ) -> ListAgentMcpToolsOutput:
        registry = self._network_registry()
        if server is not None:
            registry, owner = await self._resolve_server_owner(
                server, session_id
            )
            if owner == "control":
                return list_agent_mcp_tools_payload(registry, server)
            selected_session = self._require_session_id(session_id, server)
            if owner == "unknown":
                raise ValueError(f"Unknown agent MCP server: {server}")
            payload = await self._sessions.call_session_tool(
                "agent_mcp.list_tools",
                {"session_id": selected_session, "server": server},
            )
            return ListAgentMcpToolsOutput.model_validate(payload)

        control = list_agent_mcp_tools_payload(registry).tools
        if session_id is None:
            return ListAgentMcpToolsOutput(tools=control)
        payload = await self._sessions.call_session_tool(
            "agent_mcp.list_tools", {"session_id": session_id, "server": None}
        )
        executor = ListAgentMcpToolsOutput.model_validate(payload).tools
        control_servers = {str(row.get("server", "")) for row in control}
        executor_servers = {str(row.get("server", "")) for row in executor}
        duplicates = sorted(control_servers & executor_servers - {""})
        if duplicates:
            names = ", ".join(duplicates)
            raise ValueError(
                "Agent MCP server names must be unique across control and the "
                f"selected executor; duplicates: {names}"
            )
        return ListAgentMcpToolsOutput(tools=[*control, *executor])

    async def call_tool(
        self,
        server: str,
        tool: str,
        args: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> CallAgentMcpToolOutput:
        registry, owner = await self._resolve_server_owner(server, session_id)
        if owner == "control":
            return await call_agent_mcp_tool_payload(
                registry, server, tool, args or {}
            )
        selected_session = self._require_session_id(session_id, server)
        if owner == "unknown":
            raise ValueError(f"Unknown agent MCP server: {server}")
        payload = await self._sessions.call_session_tool(
            "agent_mcp.call_tool",
            {
                "session_id": selected_session,
                "server": server,
                "tool": tool,
                "args": args or {},
            },
        )
        return CallAgentMcpToolOutput.model_validate(payload)
