"""Authenticated WebUI terminal adapter over final executor RPC."""

from typing import Any

from fastapi import HTTPException
from pydantic import JsonValue
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from ...control.ui_executor import call_ui_executor
from ...oauth.core.context import MissingOAuthScopeError, require_oauth_scopes
from ...oauth.core.scopes import SCOPE_SHELL_EXECUTE, SCOPE_SHELL_READ
from ...protocol.terminal import (
    PERSISTENT_SHELL_MAX_COLUMNS,
    PERSISTENT_SHELL_MAX_ROWS,
    PERSISTENT_SHELL_MIN_COLUMNS,
    PERSISTENT_SHELL_MIN_ROWS,
)
from .common import bounded_text as _bounded_text
from .common import json_error as _json_error
from .terminal_protocol import (
    UI_TERMINAL_DEFAULT_LINES,
    UI_TERMINAL_METADATA_MAX_BYTES,
    UI_TERMINAL_OUTPUT_MAX_BYTES,
    UI_TERMINAL_READ_MAX_LINES,
    _bounded_int,
    _executor_id_arg,
    _normalize_kill,
    _normalize_list,
    _normalize_read,
    _normalize_start,
    _optional_text,
    _shell_id,
)

__all__ = ["UI_TERMINAL_OUTPUT_MAX_BYTES", "_normalize_read"]


def _json_ok(data: Any = None, message: str = "") -> JSONResponse:
    return JSONResponse({"ok": True, "message": message, "data": data})


def _terminal_error(exc: Exception) -> JSONResponse:
    if isinstance(exc, ConnectionError):
        return _json_error(exc, status_code=503)
    if isinstance(exc, RuntimeError):
        return _json_error(exc, status_code=502)
    return _json_error(exc)


def _require_scopes(*required: str) -> None:
    try:
        require_oauth_scopes(tuple(dict.fromkeys(required)))
    except MissingOAuthScopeError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


def _require_terminal_scopes(*, execute: bool = False) -> None:
    required = [SCOPE_SHELL_READ]
    if execute:
        required.append(SCOPE_SHELL_EXECUTE)
    _require_scopes(*required)


def _runtime(source: Request) -> Any:
    runtime = getattr(source.app.state, "control_runtime", None)
    if runtime is None:
        raise RuntimeError("WebUI terminals require the control runtime")
    return runtime


def _session_id_arg(value: Any) -> str | None:
    if value in {None, ""}:
        return None
    return _bounded_text(
        value, field="session_id", max_bytes=128, allow_empty=False
    )


def _require_session_binding(
    runtime: Any, executor_id: str, session_id: str | None
) -> None:
    if session_id is None:
        return
    record = runtime.control_state.snapshot_sessions().get(session_id)
    if record is None:
        raise ValueError(f"unknown session_id {session_id!r}")
    if str(record.executor_id) != executor_id:
        raise ValueError(
            f"session_id {session_id!r} is not bound to executor_id {executor_id!r}"
        )
    if str(record.status) != "active":
        raise ValueError(
            f"session_id {session_id!r} is {record.status}; terminal access requires an active session"
        )


async def _terminal_call(
    runtime: Any,
    executor_id: str,
    op: str,
    args: dict[str, JsonValue] | None = None,
) -> tuple[str, JsonValue]:
    return await call_ui_executor(runtime, executor_id, op, args or {})


async def _list_shells(
    runtime: Any, executor_id: str, session_id: str | None = None
) -> dict[str, Any]:
    _require_session_binding(runtime, executor_id, session_id)
    _executor_id, value = await _terminal_call(
        runtime,
        executor_id,
        "ui.terminals.list",
        {"session_id": session_id} if session_id is not None else {},
    )
    return _normalize_list(executor_id, value)


async def _start_shell(
    runtime: Any,
    executor_id: str,
    *,
    cwd: str,
    name: str | None,
    command: str | None,
    session_id: str | None = None,
) -> dict[str, Any]:
    _require_session_binding(runtime, executor_id, session_id)
    args: dict[str, JsonValue] = {"cwd": cwd, "name": name, "command": command}
    if session_id is not None:
        args["session_id"] = session_id
    _executor_id, value = await _terminal_call(
        runtime,
        executor_id,
        "ui.terminals.start",
        args,
    )
    return _normalize_start(executor_id, value)


async def _read_shell(
    runtime: Any,
    executor_id: str,
    shell_id: str,
    lines: int,
    session_id: str | None = None,
) -> dict[str, Any]:
    _require_session_binding(runtime, executor_id, session_id)
    args: dict[str, JsonValue] = {"shell_id": shell_id, "lines": lines}
    if session_id is not None:
        args["session_id"] = session_id
    _executor_id, value = await _terminal_call(
        runtime,
        executor_id,
        "ui.terminals.read",
        args,
    )
    return _normalize_read(executor_id, shell_id, lines, value)


async def _kill_shell(
    runtime: Any,
    executor_id: str,
    shell_id: str,
    session_id: str | None = None,
) -> dict[str, Any]:
    _require_session_binding(runtime, executor_id, session_id)
    args: dict[str, JsonValue] = {"shell_id": shell_id}
    if session_id is not None:
        args["session_id"] = session_id
    _executor_id, value = await _terminal_call(
        runtime,
        executor_id,
        "ui.terminals.kill",
        args,
    )
    return _normalize_kill(executor_id, shell_id, value)


async def _attach_stream(
    runtime: Any,
    executor_id: str,
    shell_id: str,
    cols: int,
    rows: int,
    session_id: str | None = None,
) -> dict[str, Any]:
    shells = await _list_shells(runtime, executor_id, session_id)
    if shell_id not in {
        str(item.get("shell_id") or "") for item in shells["shells"]
    }:
        raise ValueError("Persistent shell not found")
    grant = await runtime.stream_hub.create(executor_id)
    try:
        resolved_executor_id, value = await _terminal_call(
            runtime,
            executor_id,
            "terminal.attach",
            {
                "stream_id": grant.stream_id,
                "shell_id": shell_id,
                "cols": cols,
                "rows": rows,
            },
        )
        if resolved_executor_id != executor_id:
            raise RuntimeError("Terminal attach executor identity changed")
        if not isinstance(value, dict):
            raise RuntimeError(
                "Executor returned malformed terminal attach data"
            )
        if str(value.get("stream_id") or "") != grant.stream_id:
            raise RuntimeError(
                "Executor returned mismatched terminal stream_id"
            )
        if str(value.get("shell_id") or "") != shell_id:
            raise RuntimeError("Executor returned mismatched terminal shell_id")
        if not bool(value.get("connected")):
            raise RuntimeError("Executor did not establish terminal stream")
        backend = _optional_text(value.get("backend"), field="terminal backend")
        expires_at = await runtime.stream_hub.browser_expires_at(
            grant.stream_id
        )
        if expires_at is None:
            raise RuntimeError("Terminal stream expired before browser attach")
    except BaseException:
        await runtime.stream_hub.cancel(grant.stream_id)
        raise
    return {
        "executor_id": executor_id,
        "shell_id": shell_id,
        "stream_id": grant.stream_id,
        "browser_token": grant.browser_token,
        "expires_at": expires_at,
        "mode": "pty",
        "backend": backend,
    }


async def api_terminals(request: Request) -> Response:
    """List persistent shells for one selected executor or explicit session."""
    try:
        executor_id = _executor_id_arg(request.query_params.get("executor_id"))
        session_id = _session_id_arg(request.query_params.get("session_id"))
        _require_terminal_scopes()
        return _json_ok(
            await _list_shells(_runtime(request), executor_id, session_id)
        )
    except HTTPException:
        raise
    except Exception as exc:
        return _terminal_error(exc)


async def api_terminal_read(request: Request) -> Response:
    """Return a bounded recent snapshot from one executor-owned shell."""
    try:
        executor_id = _executor_id_arg(request.query_params.get("executor_id"))
        session_id = _session_id_arg(request.query_params.get("session_id"))
        _require_terminal_scopes()
        shell_id = _shell_id(request.query_params.get("shell_id"))
        lines = _bounded_int(
            request.query_params.get("lines"),
            default=UI_TERMINAL_DEFAULT_LINES,
            minimum=1,
            maximum=UI_TERMINAL_READ_MAX_LINES,
            label="lines",
        )
        return _json_ok(
            await _read_shell(
                _runtime(request), executor_id, shell_id, lines, session_id
            )
        )
    except HTTPException:
        raise
    except Exception as exc:
        return _terminal_error(exc)


async def api_terminal_action(request: Request) -> Response:
    """Start, attach, or terminate an executor-owned shell."""
    action = str(request.path_params.get("action") or "")
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("Request body must be a JSON object")
        runtime = _runtime(request)
        executor_id = _executor_id_arg(body.get("executor_id"))
        session_id = _session_id_arg(body.get("session_id"))
        _require_terminal_scopes(execute=True)
        match action:
            case "start":
                name = (
                    _bounded_text(body.get("name"), field="name", max_bytes=64)
                    if body.get("name") is not None
                    else None
                )
                command = (
                    _bounded_text(
                        body.get("command"),
                        field="command",
                        max_bytes=UI_TERMINAL_METADATA_MAX_BYTES,
                    )
                    if body.get("command") is not None
                    else None
                )
                result = await _start_shell(
                    runtime,
                    executor_id,
                    cwd=_bounded_text(
                        body.get("cwd"),
                        field="cwd",
                        max_bytes=UI_TERMINAL_METADATA_MAX_BYTES,
                        default=".",
                        allow_empty=False,
                    ),
                    name=name or None,
                    command=command or None,
                    session_id=session_id,
                )
            case "send" | "resize":
                raise ValueError(
                    "Interactive terminal input and resize require StreamHub attach"
                )
            case "attach":
                shell_id = _shell_id(body.get("shell_id"))
                cols = _bounded_int(
                    body.get("cols"),
                    default=120,
                    minimum=PERSISTENT_SHELL_MIN_COLUMNS,
                    maximum=PERSISTENT_SHELL_MAX_COLUMNS,
                    label="cols",
                )
                rows = _bounded_int(
                    body.get("rows"),
                    default=36,
                    minimum=PERSISTENT_SHELL_MIN_ROWS,
                    maximum=PERSISTENT_SHELL_MAX_ROWS,
                    label="rows",
                )
                result = await _attach_stream(
                    runtime, executor_id, shell_id, cols, rows, session_id
                )
            case "kill":
                result = await _kill_shell(
                    runtime,
                    executor_id,
                    _shell_id(body.get("shell_id")),
                    session_id,
                )
            case _:
                raise ValueError(f"Unsupported terminal action: {action}")
        return _json_ok(result)
    except HTTPException:
        raise
    except Exception as exc:
        return _terminal_error(exc)
