"""Version information MCP tool registry."""

from ..declarative import DeclarativeToolRegistry
from ..ops.version import version_info_execute
from ..schemas.result_models.version import VersionInfoOutput


class VersionToolRegistry(DeclarativeToolRegistry):
    """Register version-reporting tools."""

    name = "version"
    """Registry group name used for tool-surface organization."""


version_tool = VersionToolRegistry.get_tool_decorator()


@version_tool(
    annotations="read_only",
    oauth_scopes=("shell:read",),
)
async def version() -> VersionInfoOutput:
    """Return package and runtime version information."""
    return version_info_execute()
