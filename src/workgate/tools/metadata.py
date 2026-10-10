"""Transport-neutral client-facing tool metadata and safety annotations."""

from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from ..config.control import ControlConfig
from ..oauth.core.scopes import dedupe_scopes


def oauth_security_scheme(
    scopes: list[str] | tuple[str, ...],
) -> dict[str, Any]:
    """Return one OAuth MCP security scheme for the provided scopes."""
    return {"type": "oauth2", "scopes": dedupe_scopes(scopes)}


def mcp_security_meta(
    scopes: list[str] | tuple[str, ...],
    *,
    settings: ControlConfig,
) -> dict[str, Any]:
    """Describe the authentication actually enforced on this MCP transport."""
    if settings.mode == "stdio" or settings.auth_mode == "none":
        return {"securitySchemes": [{"type": "noauth"}]}
    return {"securitySchemes": [oauth_security_scheme(scopes)]}


_OPEN_WORLD_TOOL_NAMES = frozenset(
    {
        "bash",
        "browser_act",
        "browser_run_script",
        "browser_session",
        "browser_snapshot",
        "call_agent_mcp_tool",
        "create_file_link",
        "gui_action",
        "gui_list",
        "gui_state",
        "executor",
        "job",
        "kill_persistent_shell",
        "search_agent_mcp_tools",
        "inspect_agent_mcp_tool",
        "resize_persistent_shell",
        "revoke_file_link",
        "run_python_code",
        "send_persistent_shell_input",
        "session_change_workdir",
        "session_copy",
        "session_end",
        "session_start",
    }
)
_NON_DESTRUCTIVE_MUTATION_TOOL_NAMES = frozenset(
    {
        "create_file_link",
        "gui_state",
        "resize_persistent_shell",
        "session_change_workdir",
        "session_start",
    }
)


def tool_safety_annotations(
    tool_name: str, *, read_only: bool
) -> ToolAnnotations:
    """Return conservative, mode-independent MCP safety annotations."""
    open_world = tool_name in _OPEN_WORLD_TOOL_NAMES
    destructive = (
        not read_only and tool_name not in _NON_DESTRUCTIVE_MUTATION_TOOL_NAMES
    )
    return ToolAnnotations(
        readOnlyHint=read_only,
        destructiveHint=destructive,
        idempotentHint=read_only,
        openWorldHint=open_world,
    )


def install_tool_safety_annotations(mcp: FastMCP) -> None:
    """Annotate every registered tool without relaxing semantics by runtime mode."""
    for tool in mcp._tool_manager._tools.values():
        read_only = bool(tool.annotations and tool.annotations.readOnlyHint)
        tool.annotations = tool_safety_annotations(
            tool.name, read_only=read_only
        )
