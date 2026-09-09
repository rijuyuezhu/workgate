"""Combined Human UI state for one durable agent session."""

import asyncio
from typing import Any

from fastapi import HTTPException
from pydantic import ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from ...audit import audit_query_snapshot, query_audit, query_session_audit
from ...oauth.core.scopes import SCOPE_AUDIT_READ, SCOPE_REMOTE_USE
from ...ops.todo import read_todos_execute
from ...ops.utils.remote_session import call_remote_session_tool
from ...remote.tool_specs import REMOTE_WORKER_ORIGIN_HUMAN_UI
from ...schemas.result_models.todo import ReadTodosOutput
from ...tool_session.store import AgentSession
from . import audit as audit_http
from . import todos as todos_http


def _json_ok(data: Any = None, message: str = "") -> JSONResponse:
    return JSONResponse({"ok": True, "message": message, "data": data})


async def _local_snapshot(
    session: AgentSession,
    audit_args: dict[str, Any],
    *,
    selected_id: str,
) -> tuple[ReadTodosOutput, dict[str, Any]]:
    todos, audit_result = await asyncio.gather(
        asyncio.to_thread(
            read_todos_execute,
            session.session_id,
            touch_session=False,
        ),
        asyncio.to_thread(
            query_session_audit,
            session.session_id,
            **audit_args,
        ),
    )
    return todos, audit_query_snapshot(audit_result, selected_id=selected_id)


async def _remote_snapshot(
    session: AgentSession,
    audit_args: dict[str, Any],
    *,
    selected_id: str,
) -> tuple[ReadTodosOutput, dict[str, Any]]:
    data = await call_remote_session_tool(
        session,
        "ui_session_snapshot",
        {**audit_args, "selected_id": selected_id},
        audit_http._remote_timeout_s(),
        audit_origin=REMOTE_WORKER_ORIGIN_HUMAN_UI,
    )
    raw_todos = data.get("todos")
    raw_audit = data.get("audit")
    try:
        todos = ReadTodosOutput.model_validate(raw_todos)
    except ValidationError as exc:
        raise RuntimeError(
            f"Remote machine {session.machine} returned malformed todo state"
        ) from exc
    if not isinstance(raw_audit, dict):
        raise RuntimeError(
            f"Remote machine {session.machine} returned malformed audit state"
        )
    return todos, raw_audit


async def _normalize_audit_snapshot(
    machine: str,
    session_id: str,
    raw: dict[str, Any],
    request: Request,
) -> dict[str, Any]:
    result = audit_http._normalize_query_result(machine, raw)
    for entry in result["entries"]:
        entry["session"] = session_id

    selected = raw.get("entry")
    if not isinstance(selected, dict):
        return result
    detail = audit_http._normalize_entry(machine, selected)
    detail["session"] = session_id
    try:
        audit_http._require_scopes(*audit_http._detail_scopes(machine, detail))
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
    """Return Todos and bounded Audit state using one session request."""
    try:
        machine = todos_http._machine_arg(request.query_params.get("machine"))
        session_id = todos_http._session_id_arg(
            request.query_params.get("session_id")
        )
        required = [todos_http.SCOPE_SHELL_READ, SCOPE_AUDIT_READ]
        if machine != "local":
            required.append(SCOPE_REMOTE_USE)
        todos_http._require_scopes(*required)

        audit_args = audit_http._query_args(request)
        audit_args.pop("session", None)
        selected_id = audit_http._bounded_text(
            request.query_params.get("selected_id"),
            field="selected_id",
            max_bytes=audit_http.UI_AUDIT_ENTRY_ID_MAX_BYTES,
        )
        runtime, final_record = todos_http._final_session_for_machine(
            request, machine, session_id
        )
        if final_record is not None:
            todos, audit_result = await asyncio.gather(
                todos_http._read_final(runtime, session_id),
                asyncio.to_thread(
                    query_audit,
                    **{**audit_args, "session": session_id},
                ),
            )
            raw_audit = audit_query_snapshot(
                audit_result, selected_id=selected_id
            )
            payload = todos_http._final_payload(machine, final_record, todos)
        else:
            session = todos_http._session_for_machine(machine, session_id)
            if session.target == "local":
                todos, raw_audit = await _local_snapshot(
                    session,
                    audit_args,
                    selected_id=selected_id,
                )
            else:
                todos, raw_audit = await _remote_snapshot(
                    session,
                    audit_args,
                    selected_id=selected_id,
                )
            payload = todos_http._payload(machine, session, todos)

        normalized_audit = await _normalize_audit_snapshot(
            machine, session_id, raw_audit, request
        )
        payload["audit"] = audit_http._payload(
            machine,
            normalized_audit,
            scope="session",
        )
        return _json_ok(payload)
    except HTTPException:
        raise
    except ConnectionError as exc:
        return todos_http._json_error(exc, status_code=503)
    except RuntimeError as exc:
        return todos_http._json_error(exc, status_code=502)
    except Exception as exc:
        return todos_http._json_error(exc)
