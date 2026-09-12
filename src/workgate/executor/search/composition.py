"""Narrow explicit composition helpers for the Search vertical."""

from ..config import ExecutorConfig
from ..tool_session.resolver import SessionResolver
from ..tool_session.store import ToolSessionStore
from .core import SearchConfig
from .service import (
    LocalSearchRunner,
    SearchGrounding,
    SearchPathAccess,
    SearchService,
)


def build_local_search_runner(
    config: ExecutorConfig, store: ToolSessionStore
) -> LocalSearchRunner:
    """Compose a local Search runner from resolved executor policy."""
    paths = SearchPathAccess(
        workspace_root=config.workspace_root,
        allow_full_control=config.allow_full_control,
        path_denylist=config.path_denylist,
    )
    grounding = SearchGrounding(
        store=store,
        paths=paths,
        max_file_read_bytes=config.max_file_read_bytes,
    )
    return LocalSearchRunner(
        config=SearchConfig(
            rg_bin=config.rg_bin,
            max_results=config.max_grep_results,
            max_output_bytes=config.max_output_bytes,
        ),
        paths=paths,
        grounding=grounding,
    )


def build_search_service(
    config: ExecutorConfig,
    store: ToolSessionStore,
) -> SearchService:
    """Compose executor Search without caching any session binding."""
    return SearchService(
        sessions=SessionResolver(store),
        local=build_local_search_runner(config, store),
        max_glob_results=config.max_glob_results,
        max_tree_entries=config.max_tree_entries,
    )
