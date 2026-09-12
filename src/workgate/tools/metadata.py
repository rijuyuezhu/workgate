"""Transport-neutral client-facing tool metadata and safety annotations."""

from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from ..oauth.core.scopes import dedupe_scopes


def oauth_security_scheme(
    scopes: list[str] | tuple[str, ...],
) -> dict[str, Any]:
    """Return one OAuth MCP security scheme for the provided scopes."""
    return {"type": "oauth2", "scopes": dedupe_scopes(scopes)}


# Client-facing hint used by connector-compatible read-only search/fetch tools.
# This does not bypass AuthMiddleware or per-tool scope checks; it only helps
# connector-style clients discover these tools as a document-source surface.
NOAUTH_SECURITY_SCHEME = {"type": "noauth"}


def oauth_security_meta(
    scopes: list[str] | tuple[str, ...],
    *,
    connector_compatible: bool = False,
) -> dict[str, Any]:
    """Return MCP securitySchemes metadata for server-enforced OAuth scopes."""
    schemes = [oauth_security_scheme(scopes)]
    if connector_compatible:
        schemes.insert(0, NOAUTH_SECURITY_SCHEME)
    return {"securitySchemes": schemes}


_OPEN_WORLD_TOOL_NAMES = frozenset(
    {
        "bash",
        "call_agent_mcp_tool",
        "create_file_link",
        "job",
        "kill_persistent_shell",
        "list_agent_mcp_tools",
        "resize_persistent_shell",
        "revoke_file_link",
        "run_python_code",
        "send_persistent_shell_input",
        "session_change_cwd",
        "session_copy",
        "session_end",
        "session_start",
    }
)
_OPEN_WORLD_TOOL_PREFIXES = ("agent_mcp__",)
_NON_DESTRUCTIVE_MUTATION_TOOL_NAMES = frozenset(
    {
        "create_file_link",
        "resize_persistent_shell",
        "session_change_cwd",
        "session_start",
    }
)


def tool_safety_annotations(
    tool_name: str, *, read_only: bool
) -> ToolAnnotations:
    """Return conservative, mode-independent MCP safety annotations."""
    open_world = tool_name in _OPEN_WORLD_TOOL_NAMES or tool_name.startswith(
        _OPEN_WORLD_TOOL_PREFIXES
    )
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
