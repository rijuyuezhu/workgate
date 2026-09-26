"""Authenticated Human UI terminal adapter over final executor RPC."""

from typing import Any

import jwt
from fastapi import HTTPException
from pydantic import JsonValue
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.websockets import WebSocket

from ...audit import audit
from ...control.ui_executor import call_ui_executor
from ...oauth.core.context import MissingOAuthScopeError, require_oauth_scopes
from ...oauth.core.scopes import (
    SCOPE_SHELL_EXECUTE,
    SCOPE_SHELL_READ,
    scope_set,
)
from ...oauth.protocol.token_codec import validate_bearer_token
from ...protocol.terminal import (
    PERSISTENT_SHELL_MAX_COLUMNS,
    PERSISTENT_SHELL_MAX_ROWS,
    PERSISTENT_SHELL_MIN_COLUMNS,
    PERSISTENT_SHELL_MIN_ROWS,
)
from .common import bounded_text as _bounded_text
from .common import json_error as _json_error
from .session import has_valid_ui_origin, ui_session_claims
from .terminal_protocol import (
    UI_TERMINAL_DEFAULT_LINES,
    UI_TERMINAL_INPUT_MAX_BYTES,
    UI_TERMINAL_METADATA_MAX_BYTES,
    UI_TERMINAL_OUTPUT_MAX_BYTES,
    UI_TERMINAL_READ_MAX_LINES,
    UI_TERMINAL_SUBPROTOCOL,
    _bounded_int,
    _executor_id_arg,
    _normalize_kill,
    _normalize_list,
    _normalize_read,
    _normalize_resize,
    _normalize_send,
    _normalize_start,
    _optional_text,
    _shell_id,
    _websocket_protocols,
    _websocket_token,
    parse_terminal_websocket_request,
)
from .terminal_websocket import (
    TerminalWebSocketBackend,
    serve_terminal_websocket,
)

__all__ = [
    "UI_TERMINAL_INPUT_MAX_BYTES",
    "UI_TERMINAL_OUTPUT_MAX_BYTES",
    "UI_TERMINAL_SUBPROTOCOL",
    "_normalize_read",
    "_websocket_protocols",
]


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


def _runtime(source: Request | WebSocket) -> Any:
    runtime = getattr(source.app.state, "control_runtime", None)
    if runtime is None:
        raise RuntimeError("Human UI terminals require the control runtime")
    return runtime


async def _terminal_call(
    runtime: Any,
    executor_id: str,
    op: str,
    args: dict[str, JsonValue] | None = None,
) -> tuple[str, JsonValue]:
    return await call_ui_executor(runtime, executor_id, op, args or {})


async def _list_shells(runtime: Any, executor_id: str) -> dict[str, Any]:
    _executor_id, value = await _terminal_call(
        runtime, executor_id, "ui.terminals.list"
    )
    return _normalize_list(executor_id, value)


async def _start_shell(
    runtime: Any,
    executor_id: str,
    *,
    cwd: str,
    name: str | None,
    command: str | None,
) -> dict[str, Any]:
    _executor_id, value = await _terminal_call(
        runtime,
        executor_id,
        "ui.terminals.start",
        {"cwd": cwd, "name": name, "command": command},
    )
    return _normalize_start(executor_id, value)


async def _send_shell(
    runtime: Any,
    executor_id: str,
    shell_id: str,
    input_text: str,
    enter: bool,
) -> dict[str, Any]:
    _executor_id, value = await _terminal_call(
        runtime,
        executor_id,
        "ui.terminals.send",
        {"shell_id": shell_id, "input_text": input_text, "enter": enter},
    )
    return _normalize_send(executor_id, shell_id, value)


async def _resize_shell(
    runtime: Any,
    executor_id: str,
    shell_id: str,
    cols: int,
    rows: int,
) -> dict[str, Any]:
    _executor_id, value = await _terminal_call(
        runtime,
        executor_id,
        "ui.terminals.resize",
        {"shell_id": shell_id, "cols": cols, "rows": rows},
    )
    return _normalize_resize(executor_id, shell_id, cols, rows, value)


async def _read_shell(
    runtime: Any,
    executor_id: str,
    shell_id: str,
    lines: int,
) -> dict[str, Any]:
    _executor_id, value = await _terminal_call(
        runtime,
        executor_id,
        "ui.terminals.read",
        {"shell_id": shell_id, "lines": lines},
    )
    return _normalize_read(executor_id, shell_id, lines, value)


async def _kill_shell(
    runtime: Any, executor_id: str, shell_id: str
) -> dict[str, Any]:
    _executor_id, value = await _terminal_call(
        runtime,
        executor_id,
        "ui.terminals.kill",
        {"shell_id": shell_id},
    )
    return _normalize_kill(executor_id, shell_id, value)


async def _attach_stream(
    runtime: Any,
    executor_id: str,
    shell_id: str,
    cols: int,
    rows: int,
) -> dict[str, Any]:
    shells = await _list_shells(runtime, executor_id)
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


def _authorize_websocket(
    websocket: WebSocket, executor_id: str
) -> tuple[bool, int, str]:
    """Authorize a browser WebSocket without trusting localhost proxy hops."""
    runtime = _runtime(websocket)
    if runtime.config.auth_mode == "none":
        return True, 1000, ""

    token = _websocket_token(websocket)
    if token:
        try:
            claims = validate_bearer_token(token)
        except jwt.PyJWTError:
            return False, 4401, "Invalid OAuth bearer token"
    else:
        if not websocket.headers.get("origin", "").strip():
            return False, 4401, "OAuth authentication required"
        if not has_valid_ui_origin(websocket):
            return False, 4403, "Invalid Human UI WebSocket origin"
        try:
            claims = ui_session_claims(websocket)
        except jwt.PyJWTError:
            return False, 4401, "Invalid Human UI session"
        if claims is None:
            return False, 4401, "OAuth authentication required"

    granted = scope_set(str(claims.get("scope") or ""))
    required = [SCOPE_SHELL_READ, SCOPE_SHELL_EXECUTE]
    for scope in required:
        if scope not in granted:
            return False, 4403, f"Missing required OAuth scope: {scope}"
    return True, 1000, ""


async def api_terminals(request: Request) -> Response:
    """List persistent shells for one selected executor."""
    try:
        executor_id = _executor_id_arg(request.query_params.get("executor_id"))
        _require_terminal_scopes()
        return _json_ok(await _list_shells(_runtime(request), executor_id))
    except HTTPException:
        raise
    except Exception as exc:
        return _terminal_error(exc)


async def api_terminal_read(request: Request) -> Response:
    """Return a bounded recent snapshot from one executor-owned shell."""
    try:
        executor_id = _executor_id_arg(request.query_params.get("executor_id"))
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
            await _read_shell(_runtime(request), executor_id, shell_id, lines)
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
                    runtime, executor_id, shell_id, cols, rows
                )
            case "kill":
                result = await _kill_shell(
                    runtime, executor_id, _shell_id(body.get("shell_id"))
                )
            case _:
                raise ValueError(f"Unsupported terminal action: {action}")
        return _json_ok(result)
    except HTTPException:
        raise
    except Exception as exc:
        return _terminal_error(exc)


async def ui_terminal_websocket(websocket: WebSocket) -> None:
    """Serve the snapshot-only compatibility terminal WebSocket."""
    try:
        request = parse_terminal_websocket_request(
            executor_id=websocket.query_params.get("executor_id"),
            shell_id=websocket.path_params.get("shell_id"),
            mode=websocket.query_params.get("mode"),
            lines=websocket.query_params.get("lines"),
            cols=websocket.query_params.get("cols"),
            rows=websocket.query_params.get("rows"),
        )
    except ValueError as exc:
        await websocket.close(code=4400, reason=str(exc)[:120])
        return
    if request.requested_mode != "snapshot":
        await websocket.close(
            code=4406,
            reason="Raw PTY terminal attach moved to the StreamHub endpoint",
        )
        return

    runtime = _runtime(websocket)
    backend = TerminalWebSocketBackend(
        list_shells=lambda executor_id: _list_shells(runtime, executor_id),
        read_shell=lambda executor_id, shell_id, lines: _read_shell(
            runtime, executor_id, shell_id, lines
        ),
        send_shell=lambda executor_id, shell_id, input_text, enter: _send_shell(
            runtime, executor_id, shell_id, input_text, enter
        ),
        resize_shell=lambda executor_id, shell_id, cols, rows: _resize_shell(
            runtime, executor_id, shell_id, cols, rows
        ),
    )
    connections = runtime.human_ui_runtime.terminal_connections
    maximum_connections = runtime.config.ui_terminal_max_connections
    await serve_terminal_websocket(
        websocket,
        request,
        backend=backend,
        authorize=_authorize_websocket,
        reserve_connection=lambda: connections.reserve(maximum_connections),
        release_connection=connections.release,
        idle_timeout_s=runtime.config.ui_terminal_idle_timeout_s,
        audit_event=audit,
    )
