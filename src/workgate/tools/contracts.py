"""Tool registry contracts and transport-facing metadata types."""

from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from ..config.control import ControlSettingsView

type ToolHandler = Callable[[dict[str, Any]], Awaitable[Any]]

type HttpMethod = Literal["GET", "POST"]


@dataclass(frozen=True)
class HttpToolRoute:
    """HTTP endpoint metadata for a tool exposed through the REST adapter."""

    method: HttpMethod
    """HTTP verb accepted by the route."""
    path: str
    """Absolute REST path registered on the FastAPI app."""
    tool_name: str
    """Local tool name dispatched by this route."""
    timeout_cancellable: bool = True
    """Whether the REST watchdog may cancel this invocation on timeout."""


@dataclass(frozen=True)
class McpToolContext:
    """Shared MCP registration context prepared by the app assembler."""

    settings: ControlSettingsView
    """Resolved control-owned settings exposed to public tool metadata."""
    read_only_tool_annotations: ToolAnnotations
    """MCP annotation applied to read-only tools."""


class ToolRegistry:
    """Base class for one explicitly cataloged group of MCP and HTTP tools."""

    name: str = ""
    """Stable registry name used for grouping and surface checks."""

    def http_routes(self) -> Iterable[HttpToolRoute]:
        """Return REST routes provided by this registry."""
        return ()

    def http_handlers(self) -> Mapping[str, ToolHandler]:
        """Return canonical tool-name to HTTP invocation handler mappings."""
        return {}

    def register_mcp(self, mcp: FastMCP, context: McpToolContext) -> None:
        """Register MCP tools for this registry onto the provided app."""
        return None
