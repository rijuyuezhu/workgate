"""WebUI projection and actions for retained execution sessions."""

import asyncio
from typing import Any

from fastapi import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from ...oauth.core.context import MissingOAuthScopeError, require_oauth_scopes
from ...oauth.core.scopes import SCOPE_SHELL_READ, SCOPE_SHELL_WRITE
from .common import bounded_text, json_error


def _json_ok(data: Any = None, message: str = "") -> JSONResponse:
    return JSONResponse({"ok": True, "message": message, "data": data})


def _require_scopes(*, write: bool = False) -> None:
    required = [SCOPE_SHELL_READ]
    if write:
        required.append(SCOPE_SHELL_WRITE)
    try:
        require_oauth_scopes(tuple(required))
    except MissingOAuthScopeError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


def _final_session_payload(
    record: Any,
    *,
    availability: str | None = None,
    last_active_at: float | None = None,
) -> dict[str, Any]:
    status = str(record.status)
    projected_availability = availability or status
    return {
        "session_id": str(record.session_id),
        "executor_id": str(record.executor_id),
        "task_id": str(record.task_id) if record.task_id is not None else None,
        "workdir": record.workdir,
        "label": record.label,
        "status": status,
        "availability": projected_availability,
        "created_at": float(record.created_at),
        "updated_at": float(record.updated_at),
        "last_active_at": last_active_at,
    }


def _control_runtime(request: Request) -> Any:
    runtime = getattr(request.app.state, "control_runtime", None)
    if runtime is None:
        raise RuntimeError("WebUI Sessions requires the control runtime")
    return runtime


async def project_session_rows(
    runtime: Any,
    records: list[Any] | tuple[Any, ...],
) -> list[dict[str, Any]]:
    """Project retained session lifecycle/availability without executor RPC."""
    semaphore = asyncio.Semaphore(16)

    async def project(record: Any) -> dict[str, Any]:
        async with semaphore:
            (
                availability,
                last_active_at,
            ) = await runtime.session_coordinator.session_activity_projection(
                str(record.session_id)
            )
        return _final_session_payload(
            record,
            availability=availability,
            last_active_at=last_active_at,
        )

    rows = list(await asyncio.gather(*(project(record) for record in records)))
    rows.sort(
        key=lambda row: float(
            row["last_active_at"]
            if row["last_active_at"] is not None
            else row["updated_at"]
        ),
        reverse=True,
    )
    return rows


async def api_session_action(request: Request) -> Response:
    """Apply one explicit control-plane action to an execution session."""
    try:
        action = str(request.path_params.get("action") or "").casefold()
        if action != "terminate":
            return json_error(
                ValueError(f"unsupported session action: {action}"),
                status_code=404,
            )
        payload = await request.json()
        if not isinstance(payload, dict):
            raise ValueError("session action body must be a JSON object")
        session_id = bounded_text(
            payload.get("session_id"),
            field="session_id",
            max_bytes=32,
            allow_empty=False,
        )
        _require_scopes(write=True)
        runtime = _control_runtime(request)
        record = runtime.control_state.snapshot_sessions().get(session_id)
        if record is None:
            return json_error(
                ValueError(f"unknown session_id {session_id!r}"),
                status_code=404,
            )
        result = await runtime.session_coordinator.end_session(session_id)
        ended_record = runtime.control_state.snapshot_sessions().get(session_id)
        session_payload = (
            _final_session_payload(
                ended_record,
                availability="ended",
            )
            if ended_record is not None
            else {
                "session_id": session_id,
                "executor_id": str(record.executor_id),
                "status": "ended",
                "availability": "ended",
            }
        )
        return _json_ok(
            {"session": session_payload, "result": result},
            message="Session termination completed",
        )
    except HTTPException:
        raise
    except ValueError as exc:
        return json_error(exc, status_code=400)
    except RuntimeError as exc:
        return json_error(exc, status_code=502)
    except Exception as exc:
        return json_error(exc)
