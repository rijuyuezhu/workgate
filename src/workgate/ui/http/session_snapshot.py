"""Combined control-owned Human UI state for one shared session."""

import asyncio
from typing import Any

from fastapi import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from ...audit import audit_query_snapshot, query_audit
from ...oauth.core.scopes import SCOPE_AUDIT_READ
from . import audit as audit_http
from . import todos as todos_http


def _json_ok(data: Any = None, message: str = "") -> JSONResponse:
    return JSONResponse({"ok": True, "message": message, "data": data})


async def _normalize_audit_snapshot(
    session_id: str,
    raw: dict[str, Any],
    request: Request,
) -> dict[str, Any]:
    result = audit_http._normalize_query_result("control", raw)
    for entry in result["entries"]:
        entry["session"] = session_id

    selected = raw.get("entry")
    if not isinstance(selected, dict):
        return result
    detail = audit_http._normalize_entry("control", selected)
    detail["session"] = session_id
    try:
        audit_http._require_scopes(*audit_http._detail_scopes(detail))
    except HTTPException as exc:
        result["entry_error"] = str(exc.detail)
        return result
    result["entry"] = await asyncio.to_thread(
        audit_http._audit_view_image_detail,
        detail,
        audit_http.image_preview_request(request.query_params),
    )
    return result


async def api_session_snapshot(request: Request) -> Response:
    """Return control-owned Todos and Audit state for one shared session."""
    try:
        session_id = todos_http._session_id_arg(
            request.query_params.get("session_id")
        )
        todos_http._require_scopes(
            todos_http.SCOPE_SHELL_READ, SCOPE_AUDIT_READ
        )

        runtime, record = todos_http._shared_session(request, session_id)

        audit_args = audit_http._query_args(request)
        audit_args.pop("session", None)
        selected_id = audit_http._bounded_text(
            request.query_params.get("selected_id"),
            field="selected_id",
            max_bytes=audit_http.UI_AUDIT_ENTRY_ID_MAX_BYTES,
        )
        todos, audit_result = await asyncio.gather(
            runtime.todo_service.read(session_id),
            asyncio.to_thread(
                query_audit,
                **{**audit_args, "session": session_id},
            ),
        )
        raw_audit = audit_query_snapshot(audit_result, selected_id=selected_id)
        payload = todos_http._final_payload(record, todos)
        payload["audit"] = audit_http._payload(
            await _normalize_audit_snapshot(session_id, raw_audit, request),
            scope="session",
        )
        return _json_ok(payload)
    except HTTPException:
        raise
    except LookupError as exc:
        return todos_http._json_error(exc, status_code=404)
    except ValueError as exc:
        return todos_http._json_error(exc, status_code=400)
    except RuntimeError as exc:
        return todos_http._json_error(exc, status_code=502)
    except Exception as exc:
        return todos_http._json_error(exc)
