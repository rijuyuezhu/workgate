"""Search and tree-view tool registry."""

from ...schemas.input_models.search import (
    CaseSensitiveArg,
    GlobMaxResultsArg,
    GlobPatternArg,
    GrepGitignoreArg,
    GrepMaxResultsArg,
    GrepQueryArg,
    GrepSkipArg,
    RegexArg,
    SearchCwdArg,
    SearchPathsArg,
    TreeCwdArg,
    TreeDepthArg,
    TreeMaxEntriesArg,
)
from ...schemas.input_models.session import SessionIdArg
from ...schemas.result_models.search import (
    GlobSearchOutput,
    GrepSearchOutput,
    TreeViewOutput,
)
from ..contracts import McpToolContext
from ..declarative import DeclarativeToolRegistry


class SearchToolRegistry(DeclarativeToolRegistry):
    """Register search and tree-view tools."""

    name = "search"
    """Registry group name used for tool-surface organization."""


search_tool = SearchToolRegistry.get_tool_decorator()


def _tree_view_description(context: McpToolContext) -> str:
    del context
    return """Return a compact directory tree inside an execution session. Use it for broad project structure; use glob_search for filename patterns and search for content matches."""


def _glob_search_description(context: McpToolContext) -> str:
    del context
    return """Find paths by glob pattern inside an execution session. Use tree_view for directory structure and search for content matches."""


def _search_description(context: McpToolContext) -> str:
    del context
    return """Search file content on the execution session's bound executor. gitignore is respected by default, and displayed rows carry grounding for hashline_edit. Use read for known files/ranges and glob_search when only matching paths are needed."""


@search_tool(
    http_method="POST",
    http_path="/tools/tree",
    description=_tree_view_description,
    annotations="read_only",
    oauth_scopes=("shell:read",),
)
async def tree_view(
    session_id: SessionIdArg,
    cwd: TreeCwdArg = ".",
    depth: TreeDepthArg = 3,
    max_entries: TreeMaxEntriesArg = 500,
) -> TreeViewOutput:
    """Return a compact directory tree rooted at cwd inside a session."""
    del session_id, cwd, depth, max_entries
    raise RuntimeError("tree_view requires control routing")


@search_tool(
    http_method="POST",
    http_path="/tools/glob",
    description=_glob_search_description,
    annotations="read_only",
    oauth_scopes=("shell:read",),
)
async def glob_search(
    session_id: SessionIdArg,
    pattern: GlobPatternArg,
    cwd: SearchCwdArg = ".",
    max_results: GlobMaxResultsArg = 500,
) -> GlobSearchOutput:
    """Find files by glob pattern inside a session."""
    del session_id, pattern, cwd, max_results
    raise RuntimeError("glob_search requires control routing")


@search_tool(
    http_method="POST",
    http_path="/tools/search",
    description=_search_description,
    annotations="read_only",
    oauth_scopes=("shell:read",),
)
async def search(
    session_id: SessionIdArg,
    pattern: GrepQueryArg,
    paths: SearchPathsArg = None,
    regex: RegexArg = True,
    case_sensitive: CaseSensitiveArg = True,
    max_results: GrepMaxResultsArg = None,
    skip: GrepSkipArg = 0,
    gitignore: GrepGitignoreArg = True,
) -> GrepSearchOutput:
    """Search code content with optional path scopes."""
    del (
        session_id,
        pattern,
        paths,
        regex,
        case_sensitive,
        max_results,
        skip,
        gitignore,
    )
    raise RuntimeError("search requires control routing")
