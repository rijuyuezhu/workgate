"""Remote-worker-only tool dispatch.

This module bypasses tools.local_handlers/discovery so the worker does not import
the full MCP/FastAPI control-plane registry.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping
from types import MappingProxyType
from typing import Any

from workgate.remote.tool_specs import (
    REMOTE_WORKER_ORIGIN_ARG,
    REMOTE_WORKER_ORIGIN_HUMAN_UI,
    REMOTE_WORKER_ORIGIN_MODEL,
    REMOTE_WORKER_TOOL_NAMES,
)

type WorkerHandler = Callable[[dict[str, Any]], Awaitable[Any]]


def _audit_filters(args: dict[str, Any]) -> dict[str, Any]:
    return {
        "limit": int(args.get("limit") or 300),
        "event": args.get("event"),
        "operation": args.get("operation"),
        "session": args.get("session"),
        "search": args.get("search"),
        "start_ts": args.get("start_ts"),
        "end_ts": args.get("end_ts"),
        "sort": str(args.get("sort") or "desc"),
    }


async def _session_start(args: dict[str, Any]) -> Any:
    from workgate.ops.session import session_start_execute

    target = str(args.get("target") or "local")
    machine = args.get("machine")
    if target != "local" or machine not in (None, ""):
        raise ValueError(
            "remote worker session_start accepts only local worker sessions"
        )
    return await session_start_execute(
        str(args.get("workdir") or "."),
        "local",
        None,
        args.get("label"),
    )


async def _session_change_cwd(args: dict[str, Any]) -> Any:
    from workgate.ops.session import session_change_cwd_execute

    return await session_change_cwd_execute(
        str(args.get("session_id") or ""),
        str(args.get("workdir") or "."),
    )


async def _session_end(args: dict[str, Any]) -> Any:
    from workgate.ops.session import session_end_execute

    return await session_end_execute(
        str(args.get("session_id") or ""),
        force=bool(args.get("force", False)),
    )


async def _query_audit(args: dict[str, Any]) -> Any:
    from workgate.audit import (
        audit_query_snapshot,
        query_audit,
        query_session_audit,
        summarize_audit_entry,
    )

    log_session_id = str(args.get("log_session_id") or "")
    filters = _audit_filters(args)
    if log_session_id:
        result = await asyncio.to_thread(
            query_session_audit, log_session_id, **filters
        )
    else:
        result = await asyncio.to_thread(query_audit, **filters)
    if bool(args.get("snapshot", False)):
        return audit_query_snapshot(
            result, selected_id=str(args.get("selected_id") or "")
        )
    if bool(args.get("summary_only", False)):
        return {
            **result,
            "entries": [
                summarize_audit_entry(entry)
                for entry in result.get("entries", [])
                if isinstance(entry, dict)
            ],
        }
    return result


async def _get_audit_entry(args: dict[str, Any]) -> Any:
    from workgate.audit import get_audit_entry, get_session_audit_entry

    log_session_id = str(args.get("log_session_id") or "")
    entry_id = str(args.get("id") or "")
    include_full_payloads = bool(args.get("include_full_payloads", False))
    if log_session_id:
        return await asyncio.to_thread(
            get_session_audit_entry,
            log_session_id,
            entry_id,
            include_full_payloads=include_full_payloads,
        )
    return await asyncio.to_thread(
        get_audit_entry,
        entry_id,
        include_full_payloads=include_full_payloads,
    )


async def _ui_session_snapshot(args: dict[str, Any]) -> Any:
    from workgate.audit import audit_query_snapshot, query_session_audit
    from workgate.ops.todo import read_todos_execute

    session_id = str(args["session_id"])
    filters = _audit_filters(args)
    filters.pop("session", None)
    todos_task = asyncio.to_thread(
        read_todos_execute, session_id, touch_session=False
    )
    audit_task = asyncio.to_thread(query_session_audit, session_id, **filters)
    todos, audit_result = await asyncio.gather(todos_task, audit_task)
    return {
        "todos": todos.model_dump(mode="json"),
        "audit": audit_query_snapshot(
            audit_result, selected_id=str(args.get("selected_id") or "")
        ),
    }


async def _dashboard_snapshot(args: dict[str, Any]) -> Any:  # noqa: ARG001
    from workgate.ui.dashboard import dashboard_snapshot

    return await asyncio.to_thread(dashboard_snapshot)


async def _bash(args: dict[str, Any]) -> Any:
    from workgate.ops.bash import bash_execute

    return await bash_execute(
        str(args["session_id"]),
        str(args["command"]),
        str(args.get("cwd") or "."),
        args.get("timeout_s"),
        args.get("max_output_bytes"),
        args.get("env"),
        bool(args.get("async_", False)),
        bool(args.get("pty", False)),
        args.get("name"),
    )


async def _run_python_code(args: dict[str, Any]) -> Any:
    from workgate.ops.bash import run_python_code_execute

    return await run_python_code_execute(
        str(args["session_id"]),
        str(args["code"]),
        str(args.get("cwd") or "."),
        args.get("timeout_s"),
        args.get("max_output_bytes"),
        args.get("env"),
        bool(args.get("async_", False)),
        bool(args.get("pty", False)),
        args.get("name"),
    )


async def _open_terminal_bridge(args: dict[str, Any]) -> Any:
    from workgate.terminal.bridge import open_terminal_bridge_execute

    return await open_terminal_bridge_execute(
        str(args["shell_id"]),
        int(args.get("cols") or 120),
        int(args.get("rows") or 36),
    )


async def _read_terminal_bridge(args: dict[str, Any]) -> Any:
    from workgate.terminal.bridge import read_terminal_bridge_execute

    return await read_terminal_bridge_execute(
        str(args["bridge_id"]),
        int(args.get("max_bytes") or 65_536),
        int(args.get("wait_ms") or 0),
    )


async def _write_terminal_bridge(args: dict[str, Any]) -> Any:
    from workgate.terminal.bridge import write_terminal_bridge_execute

    return await write_terminal_bridge_execute(
        str(args["bridge_id"]),
        str(args.get("data_b64") or ""),
    )


async def _resize_terminal_bridge(args: dict[str, Any]) -> Any:
    from workgate.terminal.bridge import resize_terminal_bridge_execute

    return await resize_terminal_bridge_execute(
        str(args["bridge_id"]),
        int(args["cols"]),
        int(args["rows"]),
    )


async def _close_terminal_bridge(args: dict[str, Any]) -> Any:
    from workgate.terminal.bridge import close_terminal_bridge_execute

    return await close_terminal_bridge_execute(str(args["bridge_id"]))


async def _start_persistent_shell(args: dict[str, Any]) -> Any:
    from workgate.ops.shell import start_persistent_shell_execute

    return await start_persistent_shell_execute(
        str(args.get("cwd") or "."),
        str(args["name"]) if args.get("name") is not None else None,
        str(args["command"]) if args.get("command") is not None else None,
    )


async def _require_owned_persistent_shell(
    session_id: str, shell_id: str
) -> set[str]:
    from workgate.ops.shell import list_owned_persistent_shell_ids_execute

    owned = await list_owned_persistent_shell_ids_execute(session_id)
    if owned is None:
        raise RuntimeError("persistent shell ownership is currently uncertain")
    normalized = set(owned)
    if shell_id not in normalized:
        raise ValueError(
            f"shell_id {shell_id!r} is not owned by session {session_id!r}"
        )
    return normalized


async def _send_persistent_shell_input(args: dict[str, Any]) -> Any:
    from workgate.ops.shell import send_persistent_shell_input_execute

    session_id = str(args["session_id"])
    shell_id = str(args["shell_id"])
    await _require_owned_persistent_shell(session_id, shell_id)
    return await send_persistent_shell_input_execute(
        shell_id,
        str(args.get("input_text") or ""),
        bool(args.get("enter", True)),
    )


async def _resize_persistent_shell(args: dict[str, Any]) -> Any:
    from workgate.ops.shell import resize_persistent_shell_execute

    session_id = str(args["session_id"])
    shell_id = str(args["shell_id"])
    await _require_owned_persistent_shell(session_id, shell_id)
    return await resize_persistent_shell_execute(
        shell_id, int(args["cols"]), int(args["rows"])
    )


async def _read_persistent_shell_output(args: dict[str, Any]) -> Any:
    from workgate.ops.shell import read_persistent_shell_output_execute

    session_id = str(args["session_id"])
    shell_id = str(args["shell_id"])
    await _require_owned_persistent_shell(session_id, shell_id)
    preserve_ansi = args.get("preserve_ansi", False)
    if not isinstance(preserve_ansi, bool):
        raise ValueError("preserve_ansi must be a boolean")
    return await read_persistent_shell_output_execute(
        shell_id,
        int(args.get("lines") or 200),
        preserve_ansi=preserve_ansi,
    )


async def _kill_persistent_shell(args: dict[str, Any]) -> Any:
    from workgate.ops.shell import kill_persistent_shell_execute

    session_id = str(args["session_id"])
    shell_id = str(args["shell_id"])
    await _require_owned_persistent_shell(session_id, shell_id)
    return await kill_persistent_shell_execute(shell_id)


async def _list_persistent_shells(args: dict[str, Any]) -> Any:
    from workgate.ops.shell import list_persistent_shells_execute
    from workgate.schemas.result_models.shell import ListPersistentShellsOutput

    session_id = str(args["session_id"])
    from workgate.ops.shell import list_owned_persistent_shell_ids_execute

    owned = await list_owned_persistent_shell_ids_execute(session_id)
    if owned is None:
        raise RuntimeError("persistent shell ownership is currently uncertain")
    output = await list_persistent_shells_execute()
    owned_set = set(owned)
    return ListPersistentShellsOutput(
        shells=[shell for shell in output.shells if shell.shell_id in owned_set]
    )


async def _job(args: dict[str, Any]) -> Any:
    from workgate.jobs.runtime import job_local_execute

    return await job_local_execute(
        str(args["session_id"]),
        bool(args.get("list_jobs", False)),
        args.get("poll"),
        args.get("cancel"),
        args.get("retry"),
        bool(args.get("include_finished", True)),
        int(args.get("lines") or 200),
    )


async def _read_todos(args: dict[str, Any]) -> Any:
    from workgate.ops.todo import read_todos_execute

    return await asyncio.to_thread(read_todos_execute, str(args["session_id"]))


async def _write_todos(args: dict[str, Any]) -> Any:
    from workgate.ops.todo import write_todos_execute

    return await asyncio.to_thread(
        write_todos_execute,
        list(args.get("todos") or []),
        str(args["session_id"]),
        args.get("expected_revision"),
    )


async def _list_agent_skills(args: dict[str, Any]) -> Any:
    from workgate.ops.agent import list_agent_skills_execute

    return await asyncio.to_thread(
        list_agent_skills_execute, str(args["session_id"])
    )


async def _activate_agent_skill(args: dict[str, Any]) -> Any:
    from workgate.ops.agent import activate_agent_skill_execute

    return await asyncio.to_thread(
        activate_agent_skill_execute,
        str(args["name"]),
        str(args["session_id"]),
    )


async def _read_agent_skill_file(args: dict[str, Any]) -> Any:
    from workgate.ops.agent import read_agent_skill_file_execute

    return await asyncio.to_thread(
        read_agent_skill_file_execute,
        str(args["name"]),
        str(args["path"]),
        str(args["session_id"]),
    )


async def _list_files(args: dict[str, Any]) -> Any:
    from workgate.ops.files import list_files_dispatch_execute

    return await list_files_dispatch_execute(
        str(args.get("path") or "."),
        bool(args.get("recursive", False)),
        int(args.get("max_entries") or 500),
        str(args["session_id"]),
    )


async def _write_file(args: dict[str, Any]) -> Any:
    from workgate.ops.files import write_file_dispatch_execute

    expected_sha256 = args.get("expected_sha256")
    return await write_file_dispatch_execute(
        str(args["path"]),
        str(args.get("content") or ""),
        bool(args.get("overwrite", True)),
        str(args["session_id"]),
        None if expected_sha256 is None else str(expected_sha256),
    )


async def _edit_lines(args: dict[str, Any]) -> Any:
    from workgate.ops.files import edit_lines_dispatch_execute

    return await edit_lines_dispatch_execute(
        str(args["path"]),
        int(args["start_line"]),
        int(args["end_line"]),
        str(args.get("replacement") or ""),
        args.get("snapshot_id"),
        str(args["session_id"]),
    )


async def _hashline_edit(args: dict[str, Any]) -> Any:
    from workgate.ops.files import hashline_edit_dispatch_execute

    return await hashline_edit_dispatch_execute(
        str(args["input"]), str(args["session_id"])
    )


async def _apply_patch(args: dict[str, Any]) -> Any:
    from workgate.ops.patch import apply_patch_dispatch_execute

    return await apply_patch_dispatch_execute(
        str(args["patch"]),
        str(args.get("cwd") or "."),
        str(args["session_id"]),
    )


async def _delete_file_or_dir(args: dict[str, Any]) -> Any:
    from workgate.ops.files import delete_file_or_dir_dispatch_execute

    return await delete_file_or_dir_dispatch_execute(
        str(args["path"]),
        bool(args.get("recursive", False)),
        str(args["session_id"]),
    )


async def _read(args: dict[str, Any]) -> Any:
    from workgate.ops.read import read_execute

    return await read_execute(str(args["path"]), str(args["session_id"]))


async def _tree_view(args: dict[str, Any]) -> Any:
    from workgate.ops.search import tree_view_execute

    return await tree_view_execute(
        str(args["session_id"]),
        str(args.get("cwd") or "."),
        int(args.get("depth") or 3),
        int(args.get("max_entries") or 500),
    )


async def _glob_search(args: dict[str, Any]) -> Any:
    from workgate.ops.search import glob_search_execute

    return await glob_search_execute(
        str(args["session_id"]),
        str(args["pattern"]),
        str(args.get("cwd") or "."),
        int(args.get("max_results") or 500),
    )


async def _search(args: dict[str, Any]) -> Any:
    from workgate.ops.search import search_execute

    return await search_execute(
        str(args["pattern"]),
        args.get("paths"),
        ".",
        bool(args.get("regex", True)),
        bool(args.get("case_sensitive", True)),
        args.get("max_results"),
        str(args["session_id"]),
        int(args.get("skip") or 0),
        bool(args.get("gitignore", True)),
    )


async def _view_image(args: dict[str, Any]) -> Any:
    from workgate.ops.image import view_image_dispatch_execute

    return await view_image_dispatch_execute(
        str(args["path"]), str(args["session_id"])
    )


async def _workspace_search(args: dict[str, Any]) -> Any:
    from workgate.tool_session import get_tool_session_store
    from workgate.tools.ops.workspace_connector import search_execute

    get_tool_session_store().admit_active_session(str(args["session_id"]))
    return await search_execute(str(args["query"]))


async def _workspace_fetch(args: dict[str, Any]) -> Any:
    from workgate.tool_session import get_tool_session_store
    from workgate.tools.ops.workspace_connector import fetch_execute

    get_tool_session_store().admit_active_session(str(args["session_id"]))
    return await fetch_execute(str(args["id"]))


async def _secret_scan(args: dict[str, Any]) -> Any:
    from workgate.ops.secret_scan import secret_scan_execute

    return await secret_scan_execute(
        str(args.get("cwd") or "."),
        args.get("glob"),
        int(args.get("max_results") or 200),
        str(args["session_id"]),
    )


def _transfer_session_id(
    args: dict[str, Any],
    *,
    session_key: str = "session_id",
    workdir_key: str = "workdir",
) -> Any:
    """Prefer an immutable workdir binding over a mutable worker session id."""
    if bool(args.get("_workgate_unbound_temp", False)):
        return None
    return None if args.get(workdir_key) is not None else args.get(session_key)


async def _transfer_stat(args: dict[str, Any]) -> Any:
    from workgate.ops.transfer import transfer_stat

    return await asyncio.to_thread(
        transfer_stat,
        str(args["path"]),
        bool(args.get("sha256", True)),
        session_id=_transfer_session_id(args),
        workdir=args.get("workdir"),
    )


async def _transfer_copy_file(args: dict[str, Any]) -> Any:
    from workgate.ops.transfer import transfer_copy_file

    return await asyncio.to_thread(
        transfer_copy_file,
        str(args["source_path"]),
        str(args["destination_path"]),
        bool(args.get("overwrite", True)),
        args.get("chunk_size"),
        source_session_id=_transfer_session_id(
            args,
            session_key="source_session_id",
            workdir_key="source_workdir",
        ),
        destination_session_id=_transfer_session_id(
            args,
            session_key="destination_session_id",
            workdir_key="destination_workdir",
        ),
        source_workdir=args.get("source_workdir"),
        destination_workdir=args.get("destination_workdir"),
    )


async def _transfer_read_chunk(args: dict[str, Any]) -> Any:
    from workgate.ops.transfer import transfer_read_chunk

    return await asyncio.to_thread(
        transfer_read_chunk,
        str(args["path"]),
        int(args.get("offset") or 0),
        args.get("chunk_size"),
        session_id=_transfer_session_id(args),
        workdir=args.get("workdir"),
    )


async def _transfer_begin_write(args: dict[str, Any]) -> Any:
    from workgate.ops.transfer import transfer_begin_write

    return await asyncio.to_thread(
        transfer_begin_write,
        str(args["path"]),
        bool(args.get("overwrite", True)),
        args.get("expected_bytes"),
        session_id=_transfer_session_id(args),
        workdir=args.get("workdir"),
    )


async def _transfer_write_chunk(args: dict[str, Any]) -> Any:
    from workgate.ops.transfer import transfer_write_chunk

    return await asyncio.to_thread(
        transfer_write_chunk,
        str(args["path"]),
        str(args["transfer_id"]),
        int(args["offset"]),
        str(args["data_b64"]),
        args.get("expected_sha256"),
        session_id=_transfer_session_id(args),
        workdir=args.get("workdir"),
    )


async def _transfer_finish_write(args: dict[str, Any]) -> Any:
    from workgate.ops.transfer import transfer_finish_write

    return await asyncio.to_thread(
        transfer_finish_write,
        str(args["path"]),
        str(args["transfer_id"]),
        args.get("expected_bytes"),
        args.get("expected_sha256"),
        session_id=_transfer_session_id(args),
        workdir=args.get("workdir"),
    )


async def _transfer_abort_write(args: dict[str, Any]) -> Any:
    from workgate.ops.transfer import transfer_abort_write

    return await asyncio.to_thread(
        transfer_abort_write,
        str(args["path"]),
        str(args["transfer_id"]),
        session_id=_transfer_session_id(args),
        workdir=args.get("workdir"),
    )


async def _transfer_alloc_temp_path(args: dict[str, Any]) -> Any:
    from workgate.ops.transfer import transfer_alloc_temp_path

    return await asyncio.to_thread(
        transfer_alloc_temp_path,
        str(args.get("suffix") or ".bin"),
        session_id=args.get("session_id"),
    )


async def _transfer_pack_dir(args: dict[str, Any]) -> Any:
    from workgate.ops.transfer import transfer_pack_dir

    return await asyncio.to_thread(
        transfer_pack_dir,
        str(args["path"]),
        str(args.get("compression") or "gz"),
        session_id=_transfer_session_id(args),
        workdir=args.get("workdir"),
    )


async def _transfer_unpack_archive(args: dict[str, Any]) -> Any:
    from workgate.ops.transfer import transfer_unpack_archive

    return await asyncio.to_thread(
        transfer_unpack_archive,
        str(args["archive_path"]),
        str(args["dst_path"]),
        bool(args.get("overwrite", True)),
        bool(args.get("cleanup_archive", True)),
        session_id=_transfer_session_id(args),
        workdir=args.get("workdir"),
    )


async def _transfer_delete_temp_path(args: dict[str, Any]) -> Any:
    from workgate.ops.transfer import transfer_delete_temp_path

    return await asyncio.to_thread(transfer_delete_temp_path, str(args["path"]))


async def _transfer_http_upload(args: dict[str, Any]) -> Any:
    from workgate.remote_worker.http_transfer import upload_file

    return await asyncio.to_thread(
        upload_file,
        path=str(args["path"]),
        session_id=_transfer_session_id(args),
        workdir=args.get("workdir"),
        url=str(args["url"]),
        controller_url=str(args["controller_url"]),
        authorization=str(args["authorization"]),
        worker=str(args["worker"]),
        expected_bytes=int(args["expected_bytes"]),
        expected_sha256=str(args["expected_sha256"]),
        chunk_size=int(args["chunk_size"]),
        timeout_s=float(args.get("timeout_s") or 30),
    )


async def _transfer_http_download(args: dict[str, Any]) -> Any:
    from workgate.remote_worker.http_transfer import download_file

    return await asyncio.to_thread(
        download_file,
        path=str(args["path"]),
        session_id=_transfer_session_id(args),
        workdir=args.get("workdir"),
        url=str(args["url"]),
        controller_url=str(args["controller_url"]),
        authorization=str(args["authorization"]),
        worker=str(args["worker"]),
        transfer_id=str(args["transfer_id"]),
        expected_bytes=int(args["expected_bytes"]),
        expected_sha256=str(args["expected_sha256"]),
        overwrite=bool(args.get("overwrite", True)),
        chunk_size=int(args["chunk_size"]),
        timeout_s=float(args.get("timeout_s") or 30),
    )


async def _transfer_http_abort_download(args: dict[str, Any]) -> Any:
    from workgate.remote_worker.http_transfer import abort_download

    return await asyncio.to_thread(
        abort_download,
        path=str(args["path"]),
        session_id=_transfer_session_id(args),
        workdir=args.get("workdir"),
        transfer_id=str(args["transfer_id"]),
    )


_DEFAULT_WORKER_HANDLERS: Mapping[str, WorkerHandler] = MappingProxyType(
    {
        "session_start": _session_start,
        "session_change_cwd": _session_change_cwd,
        "session_end": _session_end,
        "dashboard_snapshot": _dashboard_snapshot,
        "query_audit": _query_audit,
        "get_audit_entry": _get_audit_entry,
        "ui_session_snapshot": _ui_session_snapshot,
        "read_todos": _read_todos,
        "write_todos": _write_todos,
        "list_agent_skills": _list_agent_skills,
        "activate_agent_skill": _activate_agent_skill,
        "read_agent_skill_file": _read_agent_skill_file,
        "bash": _bash,
        "run_python_code": _run_python_code,
        "open_terminal_bridge": _open_terminal_bridge,
        "read_terminal_bridge": _read_terminal_bridge,
        "write_terminal_bridge": _write_terminal_bridge,
        "resize_terminal_bridge": _resize_terminal_bridge,
        "close_terminal_bridge": _close_terminal_bridge,
        "start_persistent_shell": _start_persistent_shell,
        "send_persistent_shell_input": _send_persistent_shell_input,
        "resize_persistent_shell": _resize_persistent_shell,
        "read_persistent_shell_output": _read_persistent_shell_output,
        "kill_persistent_shell": _kill_persistent_shell,
        "list_persistent_shells": _list_persistent_shells,
        "job": _job,
        "list_files": _list_files,
        "write_file": _write_file,
        "edit_lines": _edit_lines,
        "hashline_edit": _hashline_edit,
        "apply_patch": _apply_patch,
        "delete_file_or_dir": _delete_file_or_dir,
        "read": _read,
        "tree_view": _tree_view,
        "glob_search": _glob_search,
        "search": _search,
        "view_image": _view_image,
        "workspace_search": _workspace_search,
        "fetch": _workspace_fetch,
        "secret_scan": _secret_scan,
        "transfer_stat": _transfer_stat,
        "transfer_copy_file": _transfer_copy_file,
        "transfer_read_chunk": _transfer_read_chunk,
        "transfer_begin_write": _transfer_begin_write,
        "transfer_write_chunk": _transfer_write_chunk,
        "transfer_finish_write": _transfer_finish_write,
        "transfer_abort_write": _transfer_abort_write,
        "transfer_alloc_temp_path": _transfer_alloc_temp_path,
        "transfer_pack_dir": _transfer_pack_dir,
        "transfer_unpack_archive": _transfer_unpack_archive,
        "transfer_delete_temp_path": _transfer_delete_temp_path,
        "transfer_http_upload": _transfer_http_upload,
        "transfer_http_download": _transfer_http_download,
        "transfer_http_abort_download": _transfer_http_abort_download,
    }
)

WORKER_TOOL_NAMES = frozenset(_DEFAULT_WORKER_HANDLERS)
if WORKER_TOOL_NAMES != REMOTE_WORKER_TOOL_NAMES:
    missing = sorted(REMOTE_WORKER_TOOL_NAMES - WORKER_TOOL_NAMES)
    extra = sorted(WORKER_TOOL_NAMES - REMOTE_WORKER_TOOL_NAMES)
    raise RuntimeError(
        f"remote worker handler/spec mismatch: missing={missing}, extra={extra}"
    )


class WorkerDispatcher:
    """Composition-scoped remote-worker handler map with shared audit policy."""

    def __init__(self, handlers: Mapping[str, WorkerHandler]) -> None:
        resolved = dict(handlers)
        names = frozenset(resolved)
        if names != REMOTE_WORKER_TOOL_NAMES:
            missing = sorted(REMOTE_WORKER_TOOL_NAMES - names)
            extra = sorted(names - REMOTE_WORKER_TOOL_NAMES)
            raise ValueError(
                f"remote worker handler/spec mismatch: missing={missing}, extra={extra}"
            )
        self._handlers = MappingProxyType(resolved)

    @property
    def handlers(self) -> Mapping[str, WorkerHandler]:
        """Return this dispatcher's immutable allowlisted handler map."""
        return self._handlers

    async def execute(self, tool: str, args: dict[str, Any]) -> Any:
        """Execute one allowlisted remote-worker tool."""
        if tool not in REMOTE_WORKER_TOOL_NAMES:
            raise ValueError(f"unsupported remote worker tool: {tool}")
        try:
            handler = self._handlers[tool]
        except KeyError as exc:
            raise ValueError(f"unsupported remote worker tool: {tool}") from exc
        payload = dict(args or {})
        origin = str(
            payload.pop(REMOTE_WORKER_ORIGIN_ARG, REMOTE_WORKER_ORIGIN_MODEL)
        )
        if origin == REMOTE_WORKER_ORIGIN_HUMAN_UI:
            return await handler(payload)
        from workgate.tool_session import tool_input_session_ids

        session_ids = tool_input_session_ids(payload)
        if not session_ids:
            return await handler(payload)

        from workgate.audit import (
            audit_call_context,
            audit_tool_call_end,
            audit_tool_call_start,
            new_audit_call_id,
        )
        from workgate.utils.serialization import to_jsonable

        call_id = new_audit_call_id()
        start = time.time()
        audit_tool_call_start(
            call_id=call_id,
            transport="worker",
            tool=tool,
            input=payload,
        )
        try:
            with audit_call_context(call_id, session_ids):
                result = await handler(payload)
        except BaseException as exc:
            audit_tool_call_end(
                call_id=call_id,
                transport="worker",
                tool=tool,
                ok=False,
                duration_ms=int((time.time() - start) * 1000),
                error={
                    "type": type(exc).__name__,
                    "message": str(exc),
                    "repr": repr(exc),
                },
                session_ids=session_ids,
            )
            raise
        audit_tool_call_end(
            call_id=call_id,
            transport="worker",
            tool=tool,
            ok=True,
            duration_ms=int((time.time() - start) * 1000),
            output=to_jsonable(result),
            session_ids=session_ids,
        )
        return result


def build_worker_dispatcher(
    *, handler_overrides: Mapping[str, WorkerHandler] | None = None
) -> WorkerDispatcher:
    """Build one fresh worker dispatcher with optional known-handler overrides."""
    overrides = dict(handler_overrides or {})
    unknown = set(overrides) - REMOTE_WORKER_TOOL_NAMES
    if unknown:
        names = ", ".join(sorted(unknown))
        raise ValueError(f"unknown remote worker handler override: {names}")
    return WorkerDispatcher({**_DEFAULT_WORKER_HANDLERS, **overrides})


async def execute_worker_tool(tool: str, args: dict[str, Any]) -> Any:
    """Execute one worker tool through a fresh worker dispatcher."""
    return await build_worker_dispatcher().execute(tool, args)
