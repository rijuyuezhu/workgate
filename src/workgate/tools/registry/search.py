"""Search and tree-view tool registry."""

from dataclasses import replace

from ...config.settings import Settings
from ...ops.search import (
    glob_search_execute,
    search_execute,
    tree_view_execute,
)
from ...ops.search.service import SearchService
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

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        search_service: SearchService | None = None,
    ) -> None:
        super().__init__(settings)
        self._search_service = search_service

    async def _bound_search(
        self,
        session_id: SessionIdArg,
        pattern: GrepQueryArg,
        paths: SearchPathsArg = None,
        regex: RegexArg = True,
        case_sensitive: CaseSensitiveArg = True,
        max_results: GrepMaxResultsArg = None,
        skip: GrepSkipArg = 0,
        gitignore: GrepGitignoreArg = True,
    ) -> GrepSearchOutput:
        """Invoke the composition-bound Search service."""
        if self._search_service is None:
            raise RuntimeError("Search service is not bound")
        return await self._search_service.search(
            session_id,
            pattern,
            paths,
            regex,
            case_sensitive,
            max_results,
            skip,
            gitignore,
        )

    def _enabled_tools(self):
        tools = super()._enabled_tools()
        if self._search_service is None:
            return tools
        return tuple(
            replace(
                tool,
                func=self._bound_search,
                session_admission="handler",
            )
            if tool.name == "search"
            else tool
            for tool in tools
        )


search_tool = SearchToolRegistry.get_tool_decorator()


def _tree_view_description(context: McpToolContext) -> str:
    settings = context.settings
    return f"""Return a compact directory tree inside an explicit executor-backed agent/workspace session for high-level project orientation before targeted file reads. Use tree_view when you need structure, directories, and broad layout; use glob_search when you already know filename patterns; use search when you need content matches. Pass the shared session_id returned by session_start. cwd defaults to the session workdir; any relative cwd override resolves inside that session workdir on the bound executor. Current max tree entries: {settings.max_tree_entries}."""


def _glob_search_description(context: McpToolContext) -> str:
    settings = context.settings
    return f"""Find files by glob pattern inside an explicit executor-backed agent/workspace session when you know filename patterns and need matching paths, not file contents. Use glob_search for path discovery by pattern; use tree_view for directory shape; use search for content matches with edit-grounding metadata. Pass the shared session_id returned by session_start. cwd defaults to the session workdir; any relative cwd override resolves inside that session workdir on the bound executor. Current max glob results: {settings.max_glob_results}."""


def _search_description(context: McpToolContext) -> str:
    settings = context.settings
    return f"""Search code content inside an explicit executor-backed agent/workspace session for matching lines. Use this built-in search for content discovery instead of shell grep/ripgrep when you need editable grounding, because displayed rows carry hashline grounding for hashline_edit. Use read when you already know the exact file/range, glob_search when you only need matching paths, and workspace_search/fetch when connector-compatible result shapes are useful; those tools still require the same session_id. pattern is text or regex depending on regex; paths scopes to files, directories, globs, or file line selectors such as `src/app.py:10-20,30-40`. gitignore defaults to true, so search respects .gitignore, .ignore, and related ignore rules; set gitignore=false to include ignored files. matches contains actual matched lines only. displayed_lines contains the shown editable rows and marks each row with kind=\"match\" or kind=\"context\"; numbered_content keeps the same rows in copyable `[path#snapshot_id]` plus `line:text` form that can be copied into hashline_edit. Use `skip` with the same pattern and paths to page through later actual matches when results are truncated or noisy. Use edit_lines only when you already have exact structured path/start/end/replacement data. Current max_grep_results={settings.max_grep_results}."""


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
    return await tree_view_execute(session_id, cwd, depth, max_entries)


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
    return await glob_search_execute(session_id, pattern, cwd, max_results)


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
    return await search_execute(
        pattern,
        paths,
        ".",
        regex,
        case_sensitive,
        max_results,
        session_id,
        skip,
        gitignore,
    )
