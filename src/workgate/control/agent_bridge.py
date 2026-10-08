"""Control-owned Agent Bridge routing across network and executor-local transports."""

import asyncio
from typing import Any

from ..agent_bridge.discovery import McpDiscovery
from ..agent_bridge.mcp import AgentMcpClientManager
from ..agent_bridge.models import AgentCapabilityRegistry
from ..agent_bridge.service import (
    build_network_agent_registry_from_settings,
    call_agent_mcp_tool_payload,
    list_agent_mcp_servers_payload,
    list_agent_mcp_tools_payload,
)
from ..config.control import ControlConfig
from ..schemas.result_models.agent import (
    CallAgentMcpToolOutput,
    ListAgentMcpServersOutput,
    ListAgentMcpToolsOutput,
)
from .sessions import ControlSessionCoordinator


class ControlAgentBridgeService:
    """Keep network MCP on control and route session-bound stdio MCP to executors."""

    def __init__(
        self, settings: ControlConfig, sessions: ControlSessionCoordinator
    ) -> None:
        self._settings = settings
        self._sessions = sessions
        self._discovery = McpDiscovery(self._discovery_rows)

    def _network_registry(
        self,
        *,
        probe_mcp_tools: bool = True,
        mcp_server_name: str | None = None,
    ) -> AgentCapabilityRegistry:
        return build_network_agent_registry_from_settings(
            self._settings,
            AgentMcpClientManager,
            probe_mcp_tools=probe_mcp_tools,
            mcp_server_name=mcp_server_name,
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
        registry = await asyncio.to_thread(
            self._network_registry, probe_mcp_tools=False
        )
        control_has = server in registry.mcp_servers
        if session_id is None:
            return registry, "control" if control_has else "unknown"
        payload = await self._sessions.call_session_tool(
            "agent_mcp.list_servers",
            {"session_id": session_id, "probe_mcp_tools": False},
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
        control = list_agent_mcp_servers_payload(
            await asyncio.to_thread(self._network_registry)
        ).root
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
        if server is not None:
            _, owner = await self._resolve_server_owner(server, session_id)
            if owner == "control":
                registry = await asyncio.to_thread(
                    self._network_registry, mcp_server_name=server
                )
                return list_agent_mcp_tools_payload(registry, server)
            selected_session = self._require_session_id(session_id, server)
            if owner == "unknown":
                raise ValueError(f"Unknown agent MCP server: {server}")
            payload = await self._sessions.call_session_tool(
                "agent_mcp.list_tools",
                {"session_id": selected_session, "server": server},
            )
            return ListAgentMcpToolsOutput.model_validate(payload)

        registry = await asyncio.to_thread(self._network_registry)
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

    async def _discovery_rows(
        self, session_id: str | None, server: str | None
    ) -> list[dict[str, Any]]:
        return (
            await self.list_tools(server=server, session_id=session_id)
        ).tools

    def invalidate_discovery(self, session_id: str) -> None:
        self._discovery.invalidate(session_id)

    async def search_tools(
        self,
        query: str = "",
        *,
        session_id: str | None = None,
        server: str | None = None,
        limit: int = 20,
        refresh: bool = False,
    ) -> dict[str, Any]:
        return await self._discovery.search(
            query,
            session_id=session_id,
            server=server,
            limit=limit,
            refresh=refresh,
        )

    async def inspect_tool(
        self,
        server: str,
        tool: str,
        *,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        return await self._discovery.inspect(
            server, tool, session_id=session_id
        )

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
