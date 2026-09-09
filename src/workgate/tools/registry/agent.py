"""Agent bridge MCP tool registry."""

from mcp.server.fastmcp import FastMCP

from ...agent_bridge.mcp import AgentMcpClientManager
from ...agent_bridge.models import AgentCapabilityRegistry
from ...agent_bridge.service import (
    agent_config_status_payload,
    build_agent_registry_from_settings,
    call_agent_mcp_tool_payload,
    list_agent_mcp_servers_payload,
    list_agent_mcp_tools_payload,
)
from ...agent_bridge.tools import register_agent_bridge_dynamic_tools
from ...config.settings import Settings
from ...oauth.core.scopes import SUPPORTED_OAUTH_SCOPES
from ...schemas.input_models.agent import (
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
        client_manager_factory=AgentMcpClientManager,
        allow_stdio=False,
        include_project_skills=False,
        mcp_server_types=frozenset({"http", "sse"}),
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


@agent_bridge_tool(
    http_method="POST",
    http_path="/tools/list_agent_mcp_tools",
    enabled=_agent_bridge_enabled,
    annotations="read_only",
)
async def list_agent_mcp_tools(
    server: AgentServerFilterArg = None,
    session_id: AgentMcpSessionIdArg = None,
) -> ListAgentMcpToolsOutput:
    """List tools from control-owned network MCP servers or session-bound executor stdio servers."""
    if session_id is not None:
        raise RuntimeError(
            "session-bound agent MCP listing requires control routing"
        )
    return list_agent_mcp_tools_payload(_agent_registry(), server)


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


def register_agent_bridge_dynamic_mcp(
    mcp: FastMCP, context: McpToolContext
) -> None:
    """Register dynamic MCP tools for this tool group."""
    settings = context.settings
    registry = build_agent_registry_from_settings(
        settings,
        AgentMcpClientManager,
        allow_stdio=False,
        include_project_skills=False,
        mcp_server_types=frozenset({"http", "sse"}),
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
