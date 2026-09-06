"""Executor-side composition for migrated domain services."""

from typing import Any, cast

from ..config.settings import Settings
from ..ops.files import files_config_from_settings
from ..ops.files_service import FilesService
from ..ops.search.composition import build_search_service
from ..ops.search.core import SearchPaths
from ..remote_worker.dispatch import (
    WorkerDispatcher as LegacyWorkerDispatcher,
)
from ..remote_worker.dispatch import (
    build_worker_dispatcher as build_legacy_worker_dispatcher,
)
from ..tool_session.store import ToolSessionStore


def build_executor_dispatcher_with_search(
    settings: Settings, store: ToolSessionStore
) -> LegacyWorkerDispatcher:
    """Bind executor-local Search and Files through the legacy dispatcher seam."""
    search_service = build_search_service(settings, store, remote=None)
    files_service = FilesService(
        files_config_from_settings(settings), store, remote=None
    )

    async def search_handler(args: dict[str, Any]) -> Any:
        max_results = args.get("max_results")
        return await search_service.search(
            str(args["session_id"]),
            str(args["pattern"]),
            cast(SearchPaths, args.get("paths")),
            bool(args.get("regex", True)),
            bool(args.get("case_sensitive", True)),
            int(max_results) if max_results is not None else None,
            int(args.get("skip") or 0),
            bool(args.get("gitignore", True)),
        )

    async def list_files_handler(args: dict[str, Any]) -> Any:
        return await files_service.list_files(
            str(args["session_id"]),
            str(args.get("path") or "."),
            bool(args.get("recursive", False)),
            int(args.get("max_entries") or 500),
        )

    async def write_file_handler(args: dict[str, Any]) -> Any:
        expected_sha256 = args.get("expected_sha256")
        return await files_service.write_file(
            str(args["session_id"]),
            str(args["path"]),
            str(args.get("content") or ""),
            bool(args.get("overwrite", True)),
            None if expected_sha256 is None else str(expected_sha256),
        )

    async def edit_lines_handler(args: dict[str, Any]) -> Any:
        snapshot_id = args.get("snapshot_id")
        return await files_service.edit_lines(
            str(args["session_id"]),
            str(args["path"]),
            int(args["start_line"]),
            int(args["end_line"]),
            str(args.get("replacement") or ""),
            None if snapshot_id is None else str(snapshot_id),
        )

    async def hashline_edit_handler(args: dict[str, Any]) -> Any:
        return await files_service.hashline_edit(
            str(args["session_id"]), str(args["input"])
        )

    async def delete_file_handler(args: dict[str, Any]) -> Any:
        return await files_service.delete_file_or_dir(
            str(args["session_id"]),
            str(args["path"]),
            bool(args.get("recursive", False)),
        )

    async def read_handler(args: dict[str, Any]) -> Any:
        return await files_service.read(
            str(args["session_id"]), str(args["path"])
        )

    return build_legacy_worker_dispatcher(
        handler_overrides={
            "search": search_handler,
            "list_files": list_files_handler,
            "write_file": write_file_handler,
            "edit_lines": edit_lines_handler,
            "hashline_edit": hashline_edit_handler,
            "delete_file_or_dir": delete_file_handler,
            "read": read_handler,
        }
    )
