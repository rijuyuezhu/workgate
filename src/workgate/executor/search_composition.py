"""Executor-side composition for migrated domain services."""

import asyncio
from typing import Any, cast

from ..config.settings import Settings
from ..tool_session.store import ToolSessionStore
from .agent import ExecutorAgentBridgeService
from .dispatch import ExecutorDispatcher, build_executor_dispatcher
from .files import files_config_from_settings
from .files_service import FilesService
from .search.composition import build_search_service
from .search.core import SearchPaths
from .secret_scan import SecretScanService
from .shell_service import ShellService
from .transfer_composition import build_transfer_handlers
from .workspace_connector import WorkspaceConnectorService


def build_executor_dispatcher_with_search(
    settings: Settings,
    store: ToolSessionStore,
    *,
    shell_service: ShellService | None = None,
    agent_bridge_service: ExecutorAgentBridgeService | None = None,
) -> ExecutorDispatcher:
    """Bind executor-local Search and Files into the final dispatcher."""
    search_service = build_search_service(settings, store)
    files_service = FilesService(files_config_from_settings(settings), store)
    connector_service = WorkspaceConnectorService(search_service, files_service)
    secret_scan_service = SecretScanService(
        files_service.config,
        store,
        settings.rg_bin,
        settings.max_grep_results,
    )
    if shell_service is None:
        from .config import resolve_executor_config

        shell_service = ShellService(resolve_executor_config(settings), store)
    executor_config = shell_service.config
    if agent_bridge_service is None:
        agent_bridge_service = ExecutorAgentBridgeService(executor_config)

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

    async def glob_search_handler(args: dict[str, Any]) -> Any:
        return await search_service.glob_search(
            str(args["session_id"]),
            str(args["pattern"]),
            str(args.get("cwd") or "."),
            int(args.get("max_results") or 500),
        )

    async def tree_view_handler(args: dict[str, Any]) -> Any:
        return await search_service.tree_view(
            str(args["session_id"]),
            str(args.get("cwd") or "."),
            int(args.get("depth") or 3),
            int(args.get("max_entries") or 500),
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

    async def apply_patch_handler(args: dict[str, Any]) -> Any:
        from .patch import apply_patch_execute

        return await apply_patch_execute(
            executor_config,
            store,
            str(args["patch"]),
            str(args.get("cwd") or "."),
            str(args["session_id"]),
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

    async def workspace_search_handler(args: dict[str, Any]) -> Any:
        return await connector_service.search(
            str(args["session_id"]), str(args["query"])
        )

    async def workspace_fetch_handler(args: dict[str, Any]) -> Any:
        return await connector_service.fetch(
            str(args["session_id"]), str(args["id"])
        )

    async def secret_scan_handler(args: dict[str, Any]) -> Any:
        return await secret_scan_service.scan(
            str(args["session_id"]),
            str(args.get("cwd") or "."),
            None if args.get("glob") is None else str(args["glob"]),
            int(args.get("max_results") or 200),
        )

    async def list_agent_skills_handler(args: dict[str, Any]) -> Any:
        from .agent import list_agent_skills_execute

        return await asyncio.to_thread(
            list_agent_skills_execute,
            executor_config,
            store,
            str(args["session_id"]),
        )

    async def activate_agent_skill_handler(args: dict[str, Any]) -> Any:
        from .agent import activate_agent_skill_execute

        return await asyncio.to_thread(
            activate_agent_skill_execute,
            executor_config,
            store,
            str(args["name"]),
            str(args["session_id"]),
        )

    async def read_agent_skill_file_handler(args: dict[str, Any]) -> Any:
        from .agent import read_agent_skill_file_execute

        return await asyncio.to_thread(
            read_agent_skill_file_execute,
            executor_config,
            store,
            str(args["name"]),
            str(args["path"]),
            str(args["session_id"]),
        )

    async def agent_mcp_list_servers_handler(args: dict[str, Any]) -> Any:
        del args
        return await asyncio.to_thread(agent_bridge_service.list_servers)

    async def agent_mcp_list_tools_handler(args: dict[str, Any]) -> Any:
        return await asyncio.to_thread(
            agent_bridge_service.list_tools,
            None if args.get("server") is None else str(args["server"]),
        )

    async def agent_mcp_call_tool_handler(args: dict[str, Any]) -> Any:
        return await agent_bridge_service.call_tool(
            str(args["server"]),
            str(args["tool"]),
            dict(args.get("args") or {}),
        )

    async def view_image_handler(args: dict[str, Any]) -> Any:
        from .image import view_image_execute

        return await view_image_execute(
            executor_config,
            store,
            str(args["path"]),
            str(args["session_id"]),
        )

    async def bash_handler(args: dict[str, Any]) -> Any:
        return await shell_service.bash(args)

    async def run_python_code_handler(args: dict[str, Any]) -> Any:
        return await shell_service.run_python_code(args)

    async def start_persistent_shell_handler(args: dict[str, Any]) -> Any:
        return await shell_service.start(args)

    async def send_persistent_shell_input_handler(args: dict[str, Any]) -> Any:
        return await shell_service.send(args)

    async def resize_persistent_shell_handler(args: dict[str, Any]) -> Any:
        return await shell_service.resize(args)

    async def read_persistent_shell_output_handler(args: dict[str, Any]) -> Any:
        return await shell_service.read(args)

    async def kill_persistent_shell_handler(args: dict[str, Any]) -> Any:
        return await shell_service.kill(args)

    async def list_persistent_shells_handler(args: dict[str, Any]) -> Any:
        return await shell_service.list(args)

    async def job_handler(args: dict[str, Any]) -> Any:
        return await shell_service.jobs.execute(args)

    return build_executor_dispatcher(
        handler_overrides={
            "search": search_handler,
            "glob_search": glob_search_handler,
            "tree_view": tree_view_handler,
            "list_files": list_files_handler,
            "write_file": write_file_handler,
            "edit_lines": edit_lines_handler,
            "hashline_edit": hashline_edit_handler,
            "apply_patch": apply_patch_handler,
            "delete_file_or_dir": delete_file_handler,
            "read": read_handler,
            "workspace_search": workspace_search_handler,
            "fetch": workspace_fetch_handler,
            "secret_scan": secret_scan_handler,
            "view_image": view_image_handler,
            "list_agent_skills": list_agent_skills_handler,
            "activate_agent_skill": activate_agent_skill_handler,
            "read_agent_skill_file": read_agent_skill_file_handler,
            "agent_mcp.list_servers": agent_mcp_list_servers_handler,
            "agent_mcp.list_tools": agent_mcp_list_tools_handler,
            "agent_mcp.call_tool": agent_mcp_call_tool_handler,
            "bash": bash_handler,
            "run_python_code": run_python_code_handler,
            "start_persistent_shell": start_persistent_shell_handler,
            "send_persistent_shell_input": send_persistent_shell_input_handler,
            "resize_persistent_shell": resize_persistent_shell_handler,
            "read_persistent_shell_output": read_persistent_shell_output_handler,
            "kill_persistent_shell": kill_persistent_shell_handler,
            "list_persistent_shells": list_persistent_shells_handler,
            "job": job_handler,
            **build_transfer_handlers(executor_config, store),
        }
    )
