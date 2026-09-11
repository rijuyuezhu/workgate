"""Narrow explicit composition helpers for the Search vertical."""

from ...config.settings import Settings
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
    settings: Settings, store: ToolSessionStore
) -> LocalSearchRunner:
    """Compose a local Search runner from resolved settings and one session store."""
    paths = SearchPathAccess(
        workspace_root=settings.workspace_root,
        allow_full_control=settings.allow_full_control,
        path_denylist=tuple(settings.path_denylist),
    )
    grounding = SearchGrounding(
        store=store,
        paths=paths,
        max_file_read_bytes=settings.max_file_read_bytes,
    )
    return LocalSearchRunner(
        config=SearchConfig(
            rg_bin=settings.rg_bin,
            max_results=settings.max_grep_results,
            max_output_bytes=settings.max_output_bytes,
        ),
        paths=paths,
        grounding=grounding,
    )


def build_search_service(
    settings: Settings,
    store: ToolSessionStore,
) -> SearchService:
    """Compose executor Search without caching any session binding."""
    return SearchService(
        sessions=SessionResolver(store),
        local=build_local_search_runner(settings, store),
        max_glob_results=settings.max_glob_results,
        max_tree_entries=settings.max_tree_entries,
    )
