"""Agent bridge MCP tool registry."""

from mcp.server.fastmcp import FastMCP

from ...agent_bridge.mcp import AgentMcpClientManager
from ...agent_bridge.models import AgentCapabilityRegistry
from ...agent_bridge.service import build_agent_registry_from_settings
from ...agent_bridge.tools import register_agent_bridge_dynamic_tools
from ...config.settings import Settings
from ...oauth.core.scopes import SUPPORTED_OAUTH_SCOPES
from ...ops.agent import (
    activate_agent_skill_dispatch_execute,
    agent_config_status_execute,
    call_agent_mcp_tool_execute,
    list_agent_mcp_servers_execute,
    list_agent_mcp_tools_execute,
    list_agent_skills_dispatch_execute,
    read_agent_skill_file_dispatch_execute,
)
from ...schemas.input_models.agent import (
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
    ListAgentMcpServersOutput,
    ListAgentMcpToolsOutput,
    ListAgentSkillsOutput,
    ReadAgentSkillFileOutput,
)
from ..contracts import McpToolContext
from ..declarative import DeclarativeToolRegistry
from ..metadata import oauth_security_meta


def _agent_registry() -> AgentCapabilityRegistry:
    return build_agent_registry_from_settings(
        client_manager_factory=AgentMcpClientManager
    )


def _agent_bridge_enabled(settings: Settings) -> bool:
    return settings.agent_bridge_enabled


class AgentBridgeToolRegistry(DeclarativeToolRegistry):
    """Register agent bridge tools."""

    name = "agent_bridge"
    """Registry group name used for tool-surface organization."""

    def register_mcp(self, mcp: FastMCP, context: McpToolContext) -> None:
        """Register static and dynamic agent bridge tools when enabled."""
        if not context.settings.agent_bridge_enabled:
            return
        super().register_mcp(mcp, context)
        register_agent_bridge_dynamic_mcp(mcp, context)


agent_bridge_tool = AgentBridgeToolRegistry.get_tool_decorator()


@agent_bridge_tool(
    http_method="GET",
    http_path="/tools/agent_config_status",
    enabled=_agent_bridge_enabled,
    annotations="read_only",
)
async def agent_config_status() -> AgentConfigStatusOutput:
    """Return agent bridge configuration status, discovered skills, configured MCP servers, and load errors."""
    return agent_config_status_execute(_agent_registry())


@agent_bridge_tool(
    http_method="GET",
    http_path="/tools/list_agent_skills",
    enabled=_agent_bridge_enabled,
    annotations="read_only",
)
async def list_agent_skills(
    session_id: AgentSessionIdArg = None,
) -> ListAgentSkillsOutput:
    """List Skills in project/session, managed, then global priority order without loading instructions."""
    return await list_agent_skills_dispatch_execute(session_id)


@agent_bridge_tool(
    http_method="POST",
    http_path="/tools/activate_agent_skill",
    enabled=_agent_bridge_enabled,
    annotations="read_only",
)
async def activate_agent_skill(
    name: AgentSkillNameArg,
    session_id: AgentSessionIdArg = None,
) -> ActivateAgentSkillOutput:
    """Load one exact Skill from the executor-backed session registry used by list_agent_skills."""
    return await activate_agent_skill_dispatch_execute(name, session_id)


@agent_bridge_tool(
    http_method="POST",
    http_path="/tools/read_agent_skill_file",
    enabled=_agent_bridge_enabled,
    annotations="read_only",
)
async def read_agent_skill_file(
    name: AgentSkillNameArg,
    path: AgentSkillFilePathArg,
    session_id: AgentSessionIdArg = None,
) -> ReadAgentSkillFileOutput:
    """Read a bounded related file from the same selected Skill source; activate the Skill first."""
    return await read_agent_skill_file_dispatch_execute(name, path, session_id)


@agent_bridge_tool(
    http_method="GET",
    http_path="/tools/list_agent_mcp_servers",
    enabled=_agent_bridge_enabled,
    annotations="read_only",
)
async def list_agent_mcp_servers() -> ListAgentMcpServersOutput:
    """List configured agent MCP servers. Use to find exact server names and connection status before listing or calling bridged MCP tools."""
    return list_agent_mcp_servers_execute(_agent_registry())


@agent_bridge_tool(
    http_method="POST",
    http_path="/tools/list_agent_mcp_tools",
    enabled=_agent_bridge_enabled,
    annotations="read_only",
)
async def list_agent_mcp_tools(
    server: AgentServerFilterArg = None,
) -> ListAgentMcpToolsOutput:
    """List tools exposed by configured agent MCP servers. Parameter server is optional; omit it for all servers or pass an exact server name before call_agent_mcp_tool."""
    return list_agent_mcp_tools_execute(server, _agent_registry())


@agent_bridge_tool(
    http_method="POST",
    http_path="/tools/call_agent_mcp_tool",
    enabled=_agent_bridge_enabled,
)
async def call_agent_mcp_tool(
    server: AgentServerArg, tool: AgentToolArg, args: AgentToolArgsArg = None
) -> CallAgentMcpToolOutput:
    """Call a tool on a configured agent MCP server. Parameters: server and tool must match list_agent_mcp_tools; args is a JSON object matching that tool schema, or empty for no-argument tools."""
    return await call_agent_mcp_tool_execute(
        server, tool, args, _agent_registry()
    )


def register_agent_bridge_dynamic_mcp(
    mcp: FastMCP, context: McpToolContext
) -> None:
    """Register dynamic MCP tools for this tool group."""
    settings = context.settings
    registry = build_agent_registry_from_settings(
        settings, AgentMcpClientManager
    )
    register_agent_bridge_dynamic_tools(
        mcp,
        registry,
        oauth_security_meta(SUPPORTED_OAUTH_SCOPES),
        settings.agent_mcp_probe_timeout_s,
        None if settings.agent_dynamic_mcp_tools else False,
        None if settings.agent_dynamic_skill_tools else False,
        {
            "max_skills": settings.max_skills,
            "max_skill_related_files": settings.max_skill_related_files,
            "max_skill_scan_entries": settings.max_skill_scan_entries,
            "max_skill_path_bytes": settings.max_skill_path_bytes,
            "max_skill_entry_bytes": settings.max_file_read_bytes,
        },
    )
