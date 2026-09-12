"""Authenticated Human UI Files adapter over final executor RPC."""

from typing import Any, cast

from fastapi import HTTPException
from pydantic import JsonValue
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from ...control.ui_executor import call_ui_executor
from ...oauth.core.context import MissingOAuthScopeError, require_oauth_scopes
from ...oauth.core.scopes import SCOPE_SHELL_READ, SCOPE_SHELL_WRITE
from .common import json_error as _json_error
from .image_preview import image_preview_request

UI_FILE_PATH_MAX_BYTES = 4_096


def _json_ok(data: Any = None, message: str = "") -> JSONResponse:
    return JSONResponse({"ok": True, "message": message, "data": data})


def _require_scopes(*required: str) -> None:
    try:
        require_oauth_scopes(tuple(required))
    except MissingOAuthScopeError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


def _path_arg(value: Any, *, default: str | None = None) -> str:
    path = str(value if value is not None else default or "")
    if not path:
        raise ValueError("path is required")
    if "\x00" in path:
        raise ValueError("path must not contain NUL bytes")
    if len(path.encode("utf-8")) > UI_FILE_PATH_MAX_BYTES:
        raise ValueError(f"path exceeds {UI_FILE_PATH_MAX_BYTES} encoded bytes")
    return path


def _executor_id_arg(value: Any) -> str:
    executor_id = str(value or "").strip()
    if not executor_id:
        raise ValueError("executor_id is required")
    if len(executor_id.encode("utf-8")) > 255:
        raise ValueError("executor_id exceeds 255 encoded bytes")
    return executor_id


def _require_file_scopes(*, write: bool = False) -> None:
    required = [SCOPE_SHELL_READ]
    if write:
        required.append(SCOPE_SHELL_WRITE)
    _require_scopes(*required)


def _runtime(request: Request) -> Any:
    runtime = getattr(request.app.state, "control_runtime", None)
    if runtime is None:
        raise RuntimeError("Human UI Files requires the control runtime")
    return runtime


def _payload(
    value: JsonValue, executor_id: str, *, include_executor: bool = False
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RuntimeError("executor returned malformed Human UI Files payload")
    payload = cast(dict[str, Any], dict(value))
    if include_executor:
        payload.setdefault("executor_id", executor_id)
    return payload


async def _call(
    request: Request,
    executor_id: str,
    op: str,
    args: dict[str, JsonValue],
    *,
    include_executor: bool = False,
) -> dict[str, Any]:
    resolved_executor_id, value = await call_ui_executor(
        _runtime(request), executor_id, op, args
    )
    return _payload(
        value, resolved_executor_id, include_executor=include_executor
    )


async def api_files(request: Request) -> Response:
    """List one bounded executor workspace directory for the Human UI."""
    try:
        executor_id = _executor_id_arg(request.query_params.get("executor_id"))
        _require_file_scopes()
        path = _path_arg(request.query_params.get("path"), default=".")
        return _json_ok(
            await _call(
                request,
                executor_id,
                "ui.files.list",
                {"path": path},
                include_executor=True,
            )
        )
    except HTTPException:
        raise
    except Exception as exc:
        return _json_error(exc)


async def api_file_preview(request: Request) -> Response:
    """Return a bounded executor-side directory, text, binary, or image preview."""
    try:
        executor_id = _executor_id_arg(request.query_params.get("executor_id"))
        _require_file_scopes()
        path = _path_arg(request.query_params.get("path"))
        preview = image_preview_request(request.query_params)
        args: dict[str, JsonValue] = {"path": path}
        if preview is not None:
            args.update(
                {
                    "columns": preview.columns,
                    "rows": preview.rows,
                    "cell_aspect": preview.cell_aspect,
                }
            )
        return _json_ok(
            await _call(request, executor_id, "ui.files.preview", args)
        )
    except HTTPException:
        raise
    except Exception as exc:
        return _json_error(exc)


async def api_file_content(request: Request) -> Response:
    """Return one complete bounded executor-side UTF-8 file for editing."""
    try:
        executor_id = _executor_id_arg(request.query_params.get("executor_id"))
        _require_file_scopes()
        path = _path_arg(request.query_params.get("path"))
        return _json_ok(
            await _call(
                request, executor_id, "ui.files.content", {"path": path}
            )
        )
    except HTTPException:
        raise
    except Exception as exc:
        return _json_error(exc)


async def api_file_action(request: Request) -> Response:
    """Mutate executor workspace entries through narrow executor-owned primitives."""
    action = str(request.path_params.get("action") or "")
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("Request body must be a JSON object")
        executor_id = _executor_id_arg(body.get("executor_id"))
        _require_file_scopes(write=True)
        path = _path_arg(body.get("path"))
        args: dict[str, JsonValue] = {"path": path}
        if action == "write":
            content = body.get("content")
            if not isinstance(content, str):
                raise ValueError("content must be a string")
            args.update(
                {
                    "content": content,
                    "overwrite": bool(body.get("overwrite", True)),
                    "expected_sha256": (
                        None
                        if body.get("expected_sha256") is None
                        else str(body.get("expected_sha256"))
                    ),
                }
            )
        elif action == "mkdir":
            pass
        elif action == "delete":
            args["recursive"] = bool(body.get("recursive", False))
        elif action in {"copy", "move"}:
            args["destination"] = _path_arg(body.get("destination"))
        elif action == "rename":
            name = body.get("name")
            if not isinstance(name, str):
                raise ValueError("name must be a string")
            args["name"] = name.strip()
        else:
            raise ValueError(f"Unsupported file action: {action}")
        return _json_ok(
            await _call(
                request,
                executor_id,
                f"ui.files.{action}",
                args,
                include_executor=True,
            )
        )
    except HTTPException:
        raise
    except Exception as exc:
        return _json_error(exc)
