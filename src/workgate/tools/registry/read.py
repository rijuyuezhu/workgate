"""High-level read tool registry."""

from ...schemas.input_models.read import ReadPathArg
from ...schemas.input_models.session import SessionIdArg
from ...schemas.result_models.read import ReadOutput
from ...tools.contracts import McpToolContext
from ..declarative import DeclarativeToolRegistry


class ReadToolRegistry(DeclarativeToolRegistry):
    """Register the high-level read tool declaration."""

    name = "read"


read_tool = ReadToolRegistry.get_tool_decorator()


def _read_description(context: McpToolContext) -> str:
    del context
    return """Read one file or list one directory inside an execution session. Path selectors support line ranges and raw mode; returned `[path#snapshot_id]` plus `line:text` rows can be copied into hashline_edit. Use search for content discovery and tree_view/list_files/glob_search for path discovery."""


@read_tool(
    http_method="POST",
    http_path="/tools/read",
    description=_read_description,
    annotations="read_only",
    oauth_scopes=("shell:read",),
)
async def read(
    session_id: SessionIdArg,
    path: ReadPathArg,
) -> ReadOutput:
    """Read a file or directory with optional path selector suffixes."""
    del session_id, path
    raise RuntimeError("read requires control routing")
