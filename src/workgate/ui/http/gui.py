"""Stateless WebUI observation and input for session-owned native GUI."""

import json
from typing import Any

from fastapi import HTTPException
from mcp.types import CallToolResult, ImageContent
from pydantic import TypeAdapter, ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from ...errors import GuiStaleStateError
from ...oauth.core.context import MissingOAuthScopeError, require_oauth_scopes
from ...oauth.core.scopes import SCOPE_GUI_USE
from ...schemas.input_models.gui import GuiActionsArg, GuiStateIdArg
from .common import bounded_text, json_error

_GUI_ACTION_ADAPTER = TypeAdapter(GuiActionsArg)
_GUI_STATE_ADAPTER = TypeAdapter(GuiStateIdArg)
# A 4,096-character GUI text action can exceed 8 KiB after JSON/UTF-8 encoding.
_MAX_GUI_ACTION_BODY_BYTES = 64 * 1024
_MANUAL_ACTION_TYPES = frozenset(
    {
        "click",
        "double_click",
        "right_click",
        "drag",
        "scroll",
        "focus",
        "type",
        "key",
    }
)


def _authorized_runtime(request: Request) -> Any:
    try:
        require_oauth_scopes((SCOPE_GUI_USE,))
    except MissingOAuthScopeError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    runtime = getattr(request.app.state, "control_runtime", None)
    if runtime is None:
        raise RuntimeError("WebUI GUI requires the control runtime")
    return runtime


def _authorized_session(request: Request) -> tuple[Any, str]:
    runtime = _authorized_runtime(request)
    session_id = bounded_text(
        request.query_params.get("session_id"),
        field="session_id",
        max_bytes=128,
        allow_empty=False,
    )
    return runtime, session_id


def _ok(data: dict[str, Any]) -> JSONResponse:
    return JSONResponse(
        {"ok": True, "data": data},
        headers={"Cache-Control": "no-store"},
    )


def _error(exc: Exception) -> JSONResponse:
    response = json_error(
        exc, status_code=400 if isinstance(exc, ValueError) else 502
    )
    response.headers["Cache-Control"] = "no-store"
    return response


async def api_gui_windows(request: Request) -> Response:
    """Enumerate GUI windows only on the explicitly selected active session."""
    try:
        runtime, session_id = _authorized_session(request)
        payload = await runtime.session_coordinator.call_session_tool(
            "gui_list", {"session_id": session_id}
        )
        if not isinstance(payload, dict) or not isinstance(
            payload.get("windows"), list
        ):
            raise RuntimeError("Malformed native GUI window inventory")
        return _ok(payload)
    except HTTPException:
        raise
    except Exception as exc:
        return _error(exc)


async def api_gui_state(request: Request) -> Response:
    """Capture one bounded frame; never persist or auto-refresh screenshot data."""
    try:
        runtime, session_id = _authorized_session(request)
        window_id = bounded_text(
            request.query_params.get("window_id"),
            field="window_id",
            max_bytes=1024,
            allow_empty=False,
        )
        payload = await runtime.session_coordinator.call_session_tool(
            "gui_state",
            {
                "session_id": session_id,
                "window_id": window_id,
                "screenshot": True,
                "include_elements": False,
                "max_elements": 1,
                "max_depth": 1,
            },
        )
        observed = CallToolResult.model_validate(payload)
        metadata = observed.structured_content
        frame = next(
            (
                item
                for item in observed.content
                if isinstance(item, ImageContent)
            ),
            None,
        )
        if not isinstance(metadata, dict) or frame is None:
            raise RuntimeError("Native GUI screenshot is unavailable")
        return _ok(
            {
                "window": metadata.get("window"),
                "capabilities": metadata.get("capabilities"),
                "state_id": metadata.get("state_id"),
                "state_ttl_s": metadata.get("state_ttl_s"),
                "image": {"mime_type": frame.mime_type, "data": frame.data},
            }
        )
    except HTTPException:
        raise
    except Exception as exc:
        return _error(exc)


async def api_gui_action(request: Request) -> Response:
    """Dispatch one explicit user input through the normal session-owned GUI tool."""
    try:
        # Check authorization before accepting sensitive keyboard or text data.
        runtime = _authorized_runtime(request)
        total = 0
        chunks = []
        async for chunk in request.stream():
            total += len(chunk)
            if total > _MAX_GUI_ACTION_BODY_BYTES:
                return _input_error(
                    413, "GUI input exceeds the allowed request size"
                )
            chunks.append(chunk)
        body = json.loads(b"".join(chunks))
        if not isinstance(body, dict) or set(body) != {
            "session_id",
            "window_id",
            "state_id",
            "action",
        }:
            raise ValueError("Invalid GUI input fields")
        session_id = bounded_text(
            body["session_id"],
            field="session_id",
            max_bytes=128,
            allow_empty=False,
        )
        window_id = bounded_text(
            body["window_id"],
            field="window_id",
            max_bytes=1024,
            allow_empty=False,
        )
        state_id = _GUI_STATE_ADAPTER.validate_python(body["state_id"])
        actions = _GUI_ACTION_ADAPTER.validate_python([body["action"]])
        if actions[0]["type"] not in _MANUAL_ACTION_TYPES:
            raise ValueError("Unsupported manual GUI input")
        result = await runtime.session_coordinator.call_session_tool(
            "gui_action",
            {
                "session_id": session_id,
                "window_id": window_id,
                "state_id": state_id,
                "actions": actions,
            },
        )
        if (
            not isinstance(result, dict)
            or result.get("state_consumed") is not True
        ):
            raise RuntimeError("Native GUI input result is unavailable")
        # Never return the typed value, key contents or native action records.
        return _ok({"performed": True})
    except HTTPException:
        raise
    except ValueError, ValidationError:
        return _input_error(400, "Invalid GUI input")
    except Exception as exc:
        # Native errors may quote typed content: do not expose exception text.
        return _input_error(
            409 if isinstance(exc, GuiStaleStateError) else 502,
            "GUI observation is stale; capture again"
            if isinstance(exc, GuiStaleStateError)
            else "Native GUI input failed; capture again",
        )


def _input_error(status: int, message: str) -> JSONResponse:
    return JSONResponse(
        {"ok": False, "message": message},
        status_code=status,
        headers={"Cache-Control": "no-store"},
    )
