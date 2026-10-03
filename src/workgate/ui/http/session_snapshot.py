"""Combined control-owned Human UI state for one execution session."""

import asyncio
from typing import Any

from fastapi import HTTPException
from pydantic import TypeAdapter, ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from ...audit import audit_query_snapshot, query_audit
from ...oauth.core.scopes import SCOPE_AUDIT_READ
from ...protocol.ids import SessionId
from . import audit as audit_http
from . import todos as todos_http

_SESSION_ID_ADAPTER = TypeAdapter(SessionId)


def _json_ok(data: Any = None, message: str = "") -> JSONResponse:
    return JSONResponse({"ok": True, "message": message, "data": data})


def _session_id_arg(value: Any) -> str:
    try:
        return str(
            _SESSION_ID_ADAPTER.validate_python(str(value or "").strip())
        )
    except ValidationError as exc:
        raise ValueError("session_id must be a valid sess_ id") from exc


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
    """Return execution-session state plus its attached task projection, if any."""
    try:
        session_id = _session_id_arg(request.query_params.get("session_id"))
        todos_http._require_scopes(
            todos_http.SCOPE_SHELL_READ, SCOPE_AUDIT_READ
        )
        runtime = todos_http._runtime(request)
        record = runtime.control_state.snapshot_sessions().get(session_id)
        if record is None:
            raise LookupError(f"unknown session_id {session_id!r}")

        audit_args = audit_http._query_args(request)
        audit_args.pop("session", None)
        selected_id = audit_http._bounded_text(
            request.query_params.get("selected_id"),
            field="selected_id",
            max_bytes=audit_http.UI_AUDIT_ENTRY_ID_MAX_BYTES,
        )
        task_id = str(record.task_id) if record.task_id is not None else None
        if task_id is not None:
            task_state, audit_result = await asyncio.gather(
                runtime.task_service.read_with_task(task_id),
                asyncio.to_thread(
                    query_audit,
                    **{**audit_args, "session": session_id},
                ),
            )
            todos, task = task_state
            payload = todos_http._final_payload(task_id, todos, task=task)
        else:
            audit_result = await asyncio.to_thread(
                query_audit,
                **{**audit_args, "session": session_id},
            )
            payload = {
                "task_id": None,
                "task": None,
                "updated_at": None,
                "todos": [],
            }

        payload["session_id"] = session_id
        payload["session"] = {
            "session_id": session_id,
            "task_id": task_id,
            "executor_id": str(record.executor_id),
            "workdir": record.resolved_workdir or record.requested_workdir,
            "label": record.label,
            "status": str(record.status),
            "created_at": float(record.created_at),
            "updated_at": float(record.updated_at),
        }
        raw_audit = audit_query_snapshot(audit_result, selected_id=selected_id)
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
