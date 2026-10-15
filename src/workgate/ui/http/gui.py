"""Stateless WebUI read-only views of session-owned native GUI resources."""

from typing import Any

from fastapi import HTTPException
from mcp.types import CallToolResult, ImageContent
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from ...oauth.core.context import MissingOAuthScopeError, require_oauth_scopes
from ...oauth.core.scopes import SCOPE_GUI_USE
from .common import bounded_text, json_error


def _authorized_session(request: Request) -> tuple[Any, str]:
    try:
        require_oauth_scopes((SCOPE_GUI_USE,))
    except MissingOAuthScopeError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    runtime = getattr(request.app.state, "control_runtime", None)
    if runtime is None:
        raise RuntimeError("WebUI GUI requires the control runtime")
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
                "image": {"mime_type": frame.mime_type, "data": frame.data},
            }
        )
    except HTTPException:
        raise
    except Exception as exc:
        return _error(exc)
