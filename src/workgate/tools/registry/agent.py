"""Agent bridge MCP tool registry."""

from ...agent_bridge.discovery import McpDiscovery
from ...agent_bridge.mcp import AgentMcpClientManager
from ...agent_bridge.models import AgentCapabilityRegistry
from ...agent_bridge.service import (
    agent_config_status_payload,
    build_network_agent_registry_from_settings,
    call_agent_mcp_tool_payload,
    list_agent_mcp_servers_payload,
    list_agent_mcp_tools_payload,
)
from ...config.control import ControlConfig
from ...schemas.input_models.agent import (
    AgentMcpSearchLimitArg,
    AgentMcpSearchQueryArg,
    AgentMcpSessionIdArg,
    AgentServerArg,
    AgentServerFilterArg,
    AgentSessionIdArg,
    AgentSkillFilePathArg,
    AgentSkillNameArg,
    AgentToolArg,
    AgentToolArgsArg,
)
from ...schemas.result_models.agent import (
    ActivateAgentSkillOutput,
    AgentConfigStatusOutput,
    CallAgentMcpToolOutput,
    InspectAgentMcpToolOutput,
    ListAgentMcpServersOutput,
    ListAgentSkillsOutput,
    ReadAgentSkillFileOutput,
    SearchAgentMcpToolsOutput,
)
from ..declarative import DeclarativeToolRegistry


def _agent_registry(server: str | None = None) -> AgentCapabilityRegistry:
    return build_network_agent_registry_from_settings(
        client_manager_factory=AgentMcpClientManager, mcp_server_name=server
    )


def _agent_bridge_enabled(settings: ControlConfig) -> bool:
    return settings.agent_bridge_enabled


class AgentBridgeToolRegistry(DeclarativeToolRegistry):
    """Register agent bridge tools."""

    name = "agent_bridge"
    """Registry group name used for tool-surface organization."""


agent_bridge_tool = AgentBridgeToolRegistry.get_tool_decorator()


@agent_bridge_tool(
    http_method="GET",
    http_path="/tools/agent_config_status",
    enabled=_agent_bridge_enabled,
    annotations="read_only",
)
async def agent_config_status() -> AgentConfigStatusOutput:
    """Return agent bridge configuration status, discovered skills, configured MCP servers, and load errors."""
    return agent_config_status_payload(_agent_registry())


@agent_bridge_tool(
    http_method="GET",
    http_path="/tools/list_agent_skills",
    enabled=_agent_bridge_enabled,
    annotations="read_only",
)
async def list_agent_skills(
    session_id: AgentSessionIdArg,
) -> ListAgentSkillsOutput:
    """List Skills in project/session, managed, then global priority order without loading instructions."""
    raise RuntimeError("list_agent_skills requires control executor routing")


@agent_bridge_tool(
    http_method="POST",
    http_path="/tools/activate_agent_skill",
    enabled=_agent_bridge_enabled,
    annotations="read_only",
)
async def activate_agent_skill(
    name: AgentSkillNameArg,
    session_id: AgentSessionIdArg,
) -> ActivateAgentSkillOutput:
    """Load one exact Skill from the executor-backed session registry used by list_agent_skills."""
    raise RuntimeError("activate_agent_skill requires control executor routing")


@agent_bridge_tool(
    http_method="POST",
    http_path="/tools/read_agent_skill_file",
    enabled=_agent_bridge_enabled,
    annotations="read_only",
)
async def read_agent_skill_file(
    name: AgentSkillNameArg,
    path: AgentSkillFilePathArg,
    session_id: AgentSessionIdArg,
) -> ReadAgentSkillFileOutput:
    """Read a bounded related file from the same selected Skill source; activate the Skill first."""
    raise RuntimeError(
        "read_agent_skill_file requires control executor routing"
    )


@agent_bridge_tool(
    http_method="GET",
    http_path="/tools/list_agent_mcp_servers",
    enabled=_agent_bridge_enabled,
    annotations="read_only",
)
async def list_agent_mcp_servers(
    session_id: AgentMcpSessionIdArg = None,
) -> ListAgentMcpServersOutput:
    """List control-owned network MCP servers and, with session_id, executor-owned stdio servers."""
    if session_id is not None:
        raise RuntimeError(
            "session-bound agent MCP listing requires control routing"
        )
    return list_agent_mcp_servers_payload(_agent_registry())


async def _local_search_rows(
    _session_id: str | None, server: str | None
) -> list[dict]:
    import asyncio

    registry = await asyncio.to_thread(_agent_registry, server)
    return list_agent_mcp_tools_payload(registry, server).tools


_LOCAL_DISCOVERY = McpDiscovery(_local_search_rows)


@agent_bridge_tool(
    http_method="POST",
    http_path="/tools/search_agent_mcp_tools",
    enabled=_agent_bridge_enabled,
    annotations="read_only",
)
async def search_agent_mcp_tools(
    query: AgentMcpSearchQueryArg = "",
    session_id: AgentMcpSessionIdArg = None,
    server: AgentServerFilterArg = None,
    limit: AgentMcpSearchLimitArg = 20,
    refresh: bool = False,
) -> SearchAgentMcpToolsOutput:
    """Search bounded MCP tool summaries; inspect a result for its input schema."""
    if session_id is not None:
        raise RuntimeError("session-bound MCP search requires control routing")
    return SearchAgentMcpToolsOutput(
        **await _LOCAL_DISCOVERY.search(
            query,
            server=server,
            limit=limit,
            refresh=refresh,
        )
    )


@agent_bridge_tool(
    http_method="POST",
    http_path="/tools/inspect_agent_mcp_tool",
    enabled=_agent_bridge_enabled,
    annotations="read_only",
)
async def inspect_agent_mcp_tool(
    server: AgentServerArg,
    tool: AgentToolArg,
    session_id: AgentMcpSessionIdArg = None,
) -> InspectAgentMcpToolOutput:
    """Inspect the input schema of exactly one previously discovered MCP tool."""
    if session_id is not None:
        raise RuntimeError(
            "session-bound MCP inspection requires control routing"
        )
    return InspectAgentMcpToolOutput(
        **await _LOCAL_DISCOVERY.inspect(server, tool)
    )


@agent_bridge_tool(
    http_method="POST",
    http_path="/tools/call_agent_mcp_tool",
    enabled=_agent_bridge_enabled,
)
async def call_agent_mcp_tool(
    server: AgentServerArg,
    tool: AgentToolArg,
    args: AgentToolArgsArg = None,
    session_id: AgentMcpSessionIdArg = None,
) -> CallAgentMcpToolOutput:
    """Call a control-owned network MCP tool or, with session_id, an executor-owned stdio tool."""
    if session_id is not None:
        raise RuntimeError(
            "session-bound agent MCP calls require control routing"
        )
    return await call_agent_mcp_tool_payload(
        _agent_registry(), server, tool, args or {}
    )
