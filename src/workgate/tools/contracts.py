"""Canonical MCP tool registration contracts."""

from dataclasses import dataclass

from mcp.server.mcpserver import MCPServer

from ..config.control import ControlConfig


@dataclass(frozen=True)
class McpToolContext:
    """Shared MCP registration context prepared by the app assembler."""

    settings: ControlConfig
    """Resolved control-owned settings exposed to public tool metadata."""


class ToolRegistry:
    """Base class for one explicitly cataloged MCP tool group."""

    name: str = ""
    """Stable registry name used for grouping and surface checks."""

    def register_mcp(self, mcp: MCPServer, context: McpToolContext) -> None:
        """Register MCP tools for this registry onto the provided app."""
        return None
