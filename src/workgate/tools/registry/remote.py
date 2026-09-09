"""Remote-worker tool registry."""

import asyncio
from collections.abc import Iterable, Mapping
from typing import Any

from mcp.server.fastmcp import FastMCP

from ...audit import (
    audit_query_snapshot,
    get_audit_entry,
    get_session_audit_entry,
    query_audit,
    query_session_audit,
    summarize_audit_entry,
)
from ...config.settings import Settings
from ...ops.remote import (
    remote_admin_execute,
    remote_worker_tool_execute,
)
from ...ops.shell import start_persistent_shell_execute
from ...ops.todo import read_todos_execute
from ...remote.tool_specs import REMOTE_WORKER_TOOL_SPECS
from ...schemas.input_models.remote import (
    RemoteAdminActionArg,
    RemoteAdminArgsArg,
)
from ...schemas.result_models.remote import (
    RemoteAdminOutput,
    RemoteWorkerToolOutput,
)
from ...terminal.bridge import (
    close_terminal_bridge_execute,
    open_terminal_bridge_execute,
    read_terminal_bridge_execute,
    resize_terminal_bridge_execute,
    write_terminal_bridge_execute,
)
from ...ui.dashboard import dashboard_snapshot
from ..contracts import HttpToolRoute, McpToolContext, ToolHandler
from ..declarative import DeclarativeToolRegistry


def _remote_tools_enabled(settings: Settings) -> bool:
    return settings.remote_enabled and settings.mode == "mcp"


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


async def _query_audit_handler(args: dict[str, Any]) -> dict[str, Any]:
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


async def _get_audit_entry_handler(args: dict[str, Any]) -> dict[str, Any]:
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


async def _dashboard_snapshot_handler(
    args: dict[str, Any],  # noqa: ARG001
) -> dict[str, Any]:
    return await asyncio.to_thread(dashboard_snapshot)


async def _ui_session_snapshot_handler(args: dict[str, Any]) -> dict[str, Any]:
    session_id = str(args["session_id"])
    filters = _audit_filters(args)
    filters.pop("session", None)
    todos, audit_result = await asyncio.gather(
        asyncio.to_thread(
            read_todos_execute,
            session_id,
            touch_session=False,
        ),
        asyncio.to_thread(
            query_session_audit,
            session_id,
            **filters,
        ),
    )
    return {
        "todos": todos.model_dump(mode="json"),
        "audit": audit_query_snapshot(
            audit_result, selected_id=str(args.get("selected_id") or "")
        ),
    }


async def _start_persistent_shell_handler(args: dict[str, Any]) -> Any:
    return await start_persistent_shell_execute(
        str(args.get("cwd") or "."),
        str(args["name"]) if args.get("name") is not None else None,
        str(args["command"]) if args.get("command") is not None else None,
    )


async def _open_terminal_bridge_handler(args: dict[str, Any]) -> Any:
    return await open_terminal_bridge_execute(
        str(args["shell_id"]),
        int(args.get("cols") or 120),
        int(args.get("rows") or 36),
    )


async def _read_terminal_bridge_handler(args: dict[str, Any]) -> Any:
    return await read_terminal_bridge_execute(
        str(args["bridge_id"]),
        int(args.get("max_bytes") or 65_536),
        int(args.get("wait_ms") or 0),
    )


async def _write_terminal_bridge_handler(args: dict[str, Any]) -> Any:
    return await write_terminal_bridge_execute(
        str(args["bridge_id"]),
        str(args.get("data_b64") or ""),
    )


async def _resize_terminal_bridge_handler(args: dict[str, Any]) -> Any:
    return await resize_terminal_bridge_execute(
        str(args["bridge_id"]),
        int(args["cols"]),
        int(args["rows"]),
    )


async def _close_terminal_bridge_handler(args: dict[str, Any]) -> Any:
    return await close_terminal_bridge_execute(str(args["bridge_id"]))


_REMOTE_INTERNAL_HANDLERS: Mapping[str, ToolHandler] = {
    "open_terminal_bridge": _open_terminal_bridge_handler,
    "read_terminal_bridge": _read_terminal_bridge_handler,
    "write_terminal_bridge": _write_terminal_bridge_handler,
    "resize_terminal_bridge": _resize_terminal_bridge_handler,
    "close_terminal_bridge": _close_terminal_bridge_handler,
    "start_persistent_shell": _start_persistent_shell_handler,
    "dashboard_snapshot": _dashboard_snapshot_handler,
    "query_audit": _query_audit_handler,
    "get_audit_entry": _get_audit_entry_handler,
    "ui_session_snapshot": _ui_session_snapshot_handler,
}


class RemoteToolRegistry(DeclarativeToolRegistry):
    """Register remote-worker control-plane tools."""

    name = "remote"
    """Registry group name used for tool-surface organization."""

    def http_routes(self) -> Iterable[HttpToolRoute]:
        """Return public remote REST routes when remote tools are enabled."""
        if not _remote_tools_enabled(self._settings()):
            return ()
        return (*super().http_routes(), *REMOTE_WORKER_HTTP_ROUTES)

    def http_handlers(self) -> Mapping[str, ToolHandler]:
        """Return internal UI handlers plus enabled public remote handlers."""
        if not _remote_tools_enabled(self._settings()):
            return _REMOTE_INTERNAL_HANDLERS
        return {
            **super().http_handlers(),
            **REMOTE_WORKER_HTTP_HANDLERS,
            **_REMOTE_INTERNAL_HANDLERS,
        }

    def register_mcp(self, mcp: FastMCP, context: McpToolContext) -> None:
        """Register declarative remote MCP tools when remote tools are enabled."""
        if not _remote_tools_enabled(context.settings):
            return
        super().register_mcp(mcp, context)


remote_tool = RemoteToolRegistry.get_tool_decorator()


def _remote_admin_description(context: McpToolContext) -> str:
    del context
    return """Run migration-only control-plane actions for already-enrolled legacy remote workers: action=\"list\" inspects legacy worker inventory, action=\"revoke\" removes stale or untrusted legacy trust, action=\"rename\" updates a legacy worker name, and action=\"reconnect_command\" returns a credential-free command for an existing legacy profile.

New machines pair with `workgate executor connect <control-url>`. `remote_admin` does not create enrollment invites and is not a normal machine-execution selector. Start normal work with executor-backed `session_start(workdir=..., executor_id=...)` and then use ordinary session-bound tools."""


@remote_tool(
    http_method="POST",
    http_path="/tools/remote_admin",
    description=_remote_admin_description,
    oauth_scopes=("remote:use",),
)
async def remote_admin(
    action: RemoteAdminActionArg,
    args: RemoteAdminArgsArg,
) -> RemoteAdminOutput:
    """Run a remote-worker control-plane action."""
    return await remote_admin_execute(action, args)


def _make_remote_worker_handler(
    tool_name: str,
    *,
    timeout_arg: str | None = None,
    default_timeout: int | None = None,
) -> ToolHandler:
    """Build an HTTP handler for a proxied remote-worker primitive."""

    async def handler(args: dict[str, Any]) -> RemoteWorkerToolOutput:
        timeout_s = (
            args.get(timeout_arg, default_timeout)
            if timeout_arg is not None
            else None
        )
        return await remote_worker_tool_execute(args, tool_name, timeout_s)

    return handler


REMOTE_WORKER_HTTP_ROUTES = tuple(
    HttpToolRoute("POST", spec.http_path, spec.public_name)
    for spec in REMOTE_WORKER_TOOL_SPECS
    if spec.expose_http and spec.http_path is not None
)
REMOTE_WORKER_HTTP_HANDLERS = {
    spec.public_name: _make_remote_worker_handler(
        spec.worker_tool,
        timeout_arg=spec.timeout_arg,
        default_timeout=spec.default_timeout,
    )
    for spec in REMOTE_WORKER_TOOL_SPECS
    if spec.expose_http
}
