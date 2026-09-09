"""Executor-local operation dispatch for final protocol commands."""

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from types import MappingProxyType
from typing import Any

from ..tools.machine import (
    EXECUTOR_AGENT_MCP_OPERATION_NAMES,
    MACHINE_TOOL_NAMES,
)

EXECUTOR_OPERATION_NAMES = (
    MACHINE_TOOL_NAMES | EXECUTOR_AGENT_MCP_OPERATION_NAMES | {"job"}
)

type ExecutorHandler = Callable[[dict[str, Any]], Awaitable[Any]]


async def _dashboard_snapshot(args: dict[str, Any]) -> Any:  # noqa: ARG001
    from workgate.ui.dashboard import dashboard_snapshot

    return await asyncio.to_thread(dashboard_snapshot)


async def _bash(args: dict[str, Any]) -> Any:
    del args
    raise RuntimeError("bash requires composed executor services")


async def _run_python_code(args: dict[str, Any]) -> Any:
    del args
    raise RuntimeError("run_python_code requires composed executor services")


async def _open_terminal_bridge(args: dict[str, Any]) -> Any:
    from workgate.executor.terminal.bridge import open_terminal_bridge_execute

    return await open_terminal_bridge_execute(
        str(args["shell_id"]),
        int(args.get("cols") or 120),
        int(args.get("rows") or 36),
    )


async def _read_terminal_bridge(args: dict[str, Any]) -> Any:
    from workgate.executor.terminal.bridge import read_terminal_bridge_execute

    return await read_terminal_bridge_execute(
        str(args["bridge_id"]),
        int(args.get("max_bytes") or 65_536),
        int(args.get("wait_ms") or 0),
    )


async def _write_terminal_bridge(args: dict[str, Any]) -> Any:
    from workgate.executor.terminal.bridge import write_terminal_bridge_execute

    return await write_terminal_bridge_execute(
        str(args["bridge_id"]),
        str(args.get("data_b64") or ""),
    )


async def _resize_terminal_bridge(args: dict[str, Any]) -> Any:
    from workgate.executor.terminal.bridge import resize_terminal_bridge_execute

    return await resize_terminal_bridge_execute(
        str(args["bridge_id"]),
        int(args["cols"]),
        int(args["rows"]),
    )


async def _close_terminal_bridge(args: dict[str, Any]) -> Any:
    from workgate.executor.terminal.bridge import close_terminal_bridge_execute

    return await close_terminal_bridge_execute(str(args["bridge_id"]))


async def _start_persistent_shell(args: dict[str, Any]) -> Any:
    del args
    raise RuntimeError(
        "start_persistent_shell requires composed executor services"
    )


async def _send_persistent_shell_input(args: dict[str, Any]) -> Any:
    del args
    raise RuntimeError(
        "send_persistent_shell_input requires composed executor services"
    )


async def _resize_persistent_shell(args: dict[str, Any]) -> Any:
    del args
    raise RuntimeError(
        "resize_persistent_shell requires composed executor services"
    )


async def _read_persistent_shell_output(args: dict[str, Any]) -> Any:
    del args
    raise RuntimeError(
        "read_persistent_shell_output requires composed executor services"
    )


async def _kill_persistent_shell(args: dict[str, Any]) -> Any:
    del args
    raise RuntimeError(
        "kill_persistent_shell requires composed executor services"
    )


async def _list_persistent_shells(args: dict[str, Any]) -> Any:
    del args
    raise RuntimeError(
        "list_persistent_shells requires composed executor services"
    )


async def _job(args: dict[str, Any]) -> Any:
    del args
    raise RuntimeError("job requires composed executor services")


async def _list_agent_skills(args: dict[str, Any]) -> Any:
    del args
    raise RuntimeError("list_agent_skills requires composed executor services")


async def _activate_agent_skill(args: dict[str, Any]) -> Any:
    del args
    raise RuntimeError(
        "activate_agent_skill requires composed executor services"
    )


async def _read_agent_skill_file(args: dict[str, Any]) -> Any:
    del args
    raise RuntimeError(
        "read_agent_skill_file requires composed executor services"
    )


async def _agent_mcp_list_servers(args: dict[str, Any]) -> Any:
    del args
    raise RuntimeError(
        "agent_mcp.list_servers requires composed executor services"
    )


async def _agent_mcp_list_tools(args: dict[str, Any]) -> Any:
    del args
    raise RuntimeError(
        "agent_mcp.list_tools requires composed executor services"
    )


async def _agent_mcp_call_tool(args: dict[str, Any]) -> Any:
    del args
    raise RuntimeError(
        "agent_mcp.call_tool requires composed executor services"
    )


async def _list_files(args: dict[str, Any]) -> Any:
    del args
    raise RuntimeError("list_files requires composed executor services")


async def _write_file(args: dict[str, Any]) -> Any:
    del args
    raise RuntimeError("write_file requires composed executor services")


async def _edit_lines(args: dict[str, Any]) -> Any:
    del args
    raise RuntimeError("edit_lines requires composed executor services")


async def _hashline_edit(args: dict[str, Any]) -> Any:
    del args
    raise RuntimeError("hashline_edit requires composed executor services")


async def _apply_patch(args: dict[str, Any]) -> Any:
    del args
    raise RuntimeError("apply_patch requires composed executor services")


async def _delete_file_or_dir(args: dict[str, Any]) -> Any:
    del args
    raise RuntimeError("delete_file_or_dir requires composed executor services")


async def _read(args: dict[str, Any]) -> Any:
    del args
    raise RuntimeError("read requires composed executor services")


async def _tree_view(args: dict[str, Any]) -> Any:
    del args
    raise RuntimeError("tree_view requires composed executor services")


async def _glob_search(args: dict[str, Any]) -> Any:
    del args
    raise RuntimeError("glob_search requires composed executor services")


async def _search(args: dict[str, Any]) -> Any:
    del args
    raise RuntimeError("search requires composed executor services")


async def _view_image(args: dict[str, Any]) -> Any:
    del args
    raise RuntimeError("view_image requires composed executor services")


async def _workspace_search(args: dict[str, Any]) -> Any:
    del args
    raise RuntimeError("workspace_search requires composed executor services")


async def _workspace_fetch(args: dict[str, Any]) -> Any:
    del args
    raise RuntimeError("fetch requires composed executor services")


async def _secret_scan(args: dict[str, Any]) -> Any:
    del args
    raise RuntimeError("secret_scan requires composed executor services")


async def _transfer_stat(args: dict[str, Any]) -> Any:
    del args
    raise RuntimeError("transfer_stat requires composed executor services")


async def _transfer_copy_file(args: dict[str, Any]) -> Any:
    del args
    raise RuntimeError("transfer_copy_file requires composed executor services")


async def _transfer_read_chunk(args: dict[str, Any]) -> Any:
    del args
    raise RuntimeError(
        "transfer_read_chunk requires composed executor services"
    )


async def _transfer_begin_write(args: dict[str, Any]) -> Any:
    del args
    raise RuntimeError(
        "transfer_begin_write requires composed executor services"
    )


async def _transfer_write_chunk(args: dict[str, Any]) -> Any:
    del args
    raise RuntimeError(
        "transfer_write_chunk requires composed executor services"
    )


async def _transfer_finish_write(args: dict[str, Any]) -> Any:
    del args
    raise RuntimeError(
        "transfer_finish_write requires composed executor services"
    )


async def _transfer_abort_write(args: dict[str, Any]) -> Any:
    del args
    raise RuntimeError(
        "transfer_abort_write requires composed executor services"
    )


async def _transfer_alloc_temp_path(args: dict[str, Any]) -> Any:
    del args
    raise RuntimeError(
        "transfer_alloc_temp_path requires composed executor services"
    )


async def _transfer_pack_dir(args: dict[str, Any]) -> Any:
    del args
    raise RuntimeError("transfer_pack_dir requires composed executor services")


async def _transfer_unpack_archive(args: dict[str, Any]) -> Any:
    del args
    raise RuntimeError(
        "transfer_unpack_archive requires composed executor services"
    )


async def _transfer_delete_temp_path(args: dict[str, Any]) -> Any:
    del args
    raise RuntimeError(
        "transfer_delete_temp_path requires composed executor services"
    )


_DEFAULT_EXECUTOR_HANDLERS: Mapping[str, ExecutorHandler] = MappingProxyType(
    {
        "activate_agent_skill": _activate_agent_skill,
        "agent_mcp.call_tool": _agent_mcp_call_tool,
        "agent_mcp.list_servers": _agent_mcp_list_servers,
        "agent_mcp.list_tools": _agent_mcp_list_tools,
        "apply_patch": _apply_patch,
        "bash": _bash,
        "close_terminal_bridge": _close_terminal_bridge,
        "dashboard_snapshot": _dashboard_snapshot,
        "delete_file_or_dir": _delete_file_or_dir,
        "edit_lines": _edit_lines,
        "fetch": _workspace_fetch,
        "glob_search": _glob_search,
        "hashline_edit": _hashline_edit,
        "job": _job,
        "kill_persistent_shell": _kill_persistent_shell,
        "list_agent_skills": _list_agent_skills,
        "list_files": _list_files,
        "list_persistent_shells": _list_persistent_shells,
        "open_terminal_bridge": _open_terminal_bridge,
        "read": _read,
        "read_agent_skill_file": _read_agent_skill_file,
        "read_persistent_shell_output": _read_persistent_shell_output,
        "read_terminal_bridge": _read_terminal_bridge,
        "resize_persistent_shell": _resize_persistent_shell,
        "resize_terminal_bridge": _resize_terminal_bridge,
        "run_python_code": _run_python_code,
        "search": _search,
        "secret_scan": _secret_scan,
        "send_persistent_shell_input": _send_persistent_shell_input,
        "start_persistent_shell": _start_persistent_shell,
        "transfer_abort_write": _transfer_abort_write,
        "transfer_alloc_temp_path": _transfer_alloc_temp_path,
        "transfer_begin_write": _transfer_begin_write,
        "transfer_copy_file": _transfer_copy_file,
        "transfer_delete_temp_path": _transfer_delete_temp_path,
        "transfer_finish_write": _transfer_finish_write,
        "transfer_pack_dir": _transfer_pack_dir,
        "transfer_read_chunk": _transfer_read_chunk,
        "transfer_stat": _transfer_stat,
        "transfer_unpack_archive": _transfer_unpack_archive,
        "transfer_write_chunk": _transfer_write_chunk,
        "tree_view": _tree_view,
        "view_image": _view_image,
        "workspace_search": _workspace_search,
        "write_file": _write_file,
        "write_terminal_bridge": _write_terminal_bridge,
    }
)

if frozenset(_DEFAULT_EXECUTOR_HANDLERS) != EXECUTOR_OPERATION_NAMES:
    missing = sorted(
        EXECUTOR_OPERATION_NAMES - _DEFAULT_EXECUTOR_HANDLERS.keys()
    )
    extra = sorted(_DEFAULT_EXECUTOR_HANDLERS.keys() - EXECUTOR_OPERATION_NAMES)
    raise RuntimeError(
        f"executor handler/tool classification mismatch: missing={missing}, extra={extra}"
    )


class ExecutorDispatcher:
    """Immutable executor operation table with no control-plane compatibility policy."""

    def __init__(self, handlers: Mapping[str, ExecutorHandler]) -> None:
        resolved = dict(handlers)
        names = frozenset(resolved)
        if names != EXECUTOR_OPERATION_NAMES:
            missing = sorted(EXECUTOR_OPERATION_NAMES - names)
            extra = sorted(names - EXECUTOR_OPERATION_NAMES)
            raise ValueError(
                f"executor handler/tool classification mismatch: missing={missing}, extra={extra}"
            )
        self._handlers = MappingProxyType(resolved)

    @property
    def handlers(self) -> Mapping[str, ExecutorHandler]:
        return self._handlers

    async def execute(self, op: str, args: dict[str, Any]) -> Any:
        try:
            handler = self._handlers[op]
        except KeyError as exc:
            raise ValueError(f"unsupported executor operation: {op}") from exc
        return await handler(dict(args or {}))


def build_executor_dispatcher(
    *, handler_overrides: Mapping[str, ExecutorHandler] | None = None
) -> ExecutorDispatcher:
    """Build one executor-local dispatcher with optional known-handler overrides."""
    overrides = dict(handler_overrides or {})
    unknown = set(overrides) - EXECUTOR_OPERATION_NAMES
    if unknown:
        names = ", ".join(sorted(unknown))
        raise ValueError(f"unknown executor handler override: {names}")
    return ExecutorDispatcher({**_DEFAULT_EXECUTOR_HANDLERS, **overrides})


async def execute_executor_tool(op: str, args: dict[str, Any]) -> Any:
    """Execute one executor operation through a fresh default dispatcher."""
    return await build_executor_dispatcher().execute(op, args)
