"""Tool handler catalog and dispatch helpers."""

from collections.abc import Mapping
from typing import Any

from .catalog import ToolCatalog
from .contracts import ToolHandler


class UnknownToolError(LookupError):
    """Raised when a tool name has no registered invocation handler."""


def tool_handlers(catalog: ToolCatalog) -> Mapping[str, ToolHandler]:
    """Return tool handlers from one explicit catalog composition."""
    return catalog.handlers()


async def call_tool(
    tool_name: str,
    args: dict[str, Any] | None = None,
    *,
    catalog: ToolCatalog,
) -> Any:
    """Invoke a tool by canonical public tool name from an explicit catalog."""
    try:
        handler = tool_handlers(catalog)[tool_name]
    except KeyError as exc:
        raise UnknownToolError(f"Unknown tool: {tool_name}") from exc
    return await handler(args or {})
