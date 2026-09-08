"""Authenticated Human UI inventory for durable agent/workspace sessions."""

import asyncio
import time
from dataclasses import asdict
from typing import Any

from fastapi import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from ...oauth.core.context import MissingOAuthScopeError, require_oauth_scopes
from ...oauth.core.scopes import (
    SCOPE_REMOTE_USE,
    SCOPE_SHELL_READ,
    SCOPE_SHELL_WRITE,
)
from ...tool_session.store import (
    SESSION_ACTIVE_WINDOW_S,
    AgentSession,
    get_tool_session_store,
)
from .common import bounded_text, json_error, require_remote_machine

UI_SESSION_MACHINE_MAX_BYTES = 255
UI_SESSION_MAX_ENTRIES = 2_000


def _json_ok(data: Any = None, message: str = "") -> JSONResponse:
    return JSONResponse({"ok": True, "message": message, "data": data})


def _machine_arg(value: Any) -> str:
    return bounded_text(
        value,
        field="machine",
        max_bytes=UI_SESSION_MACHINE_MAX_BYTES,
        default="local",
        allow_empty=False,
    )


def _require_scopes(machine: str, *, write: bool = False) -> None:
    required = [SCOPE_SHELL_READ]
    if write:
        required.append(SCOPE_SHELL_WRITE)
    if machine != "local":
        required.append(SCOPE_REMOTE_USE)
    try:
        require_oauth_scopes(tuple(required))
    except MissingOAuthScopeError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


def _belongs_to_machine(session: AgentSession, machine: str) -> bool:
    if machine == "local":
        return session.target == "local"
    return session.target == "remote" and session.machine == machine


def _session_payload(session: AgentSession, *, now: float) -> dict[str, Any]:
    payload = asdict(session)
    payload.pop("worker_session_id", None)
    payload["active"] = session.updated_at >= now - SESSION_ACTIVE_WINDOW_S
    payload["termination_requested"] = (
        session.termination_requested_at is not None
    )
    return payload


def _final_session_payload(
    record: Any,
    *,
    now: float,
    availability: str | None = None,
    last_active_at: float | None = None,
) -> dict[str, Any]:
    status = str(record.status)
    projected_availability = availability or status
    activity_known = last_active_at is not None
    return {
        "session_id": str(record.session_id),
        "executor_id": str(record.executor_id),
        "target": None,
        "machine": None,
        "workdir": record.resolved_workdir_display or record.requested_workdir,
        "requested_workdir": record.requested_workdir,
        "label": record.label,
        "status": status,
        "availability": projected_availability,
        "created_at": float(record.created_at),
        "updated_at": float(record.updated_at),
        "last_active_at": last_active_at,
        "activity_known": activity_known,
        "active": status == "active"
        and projected_availability == "available"
        and activity_known
        and last_active_at >= now - SESSION_ACTIVE_WINDOW_S,
        "termination_requested": status in {"terminating", "ended"},
    }


def _row_sort_timestamp(row: dict[str, Any]) -> float:
    value = row.get("last_active_at")
    if value is None:
        value = row.get("updated_at", 0.0)
    return float(value)


def _row_visible_by_default(row: dict[str, Any]) -> bool:
    if row.get("status") == "active" and row.get("availability") not in {
        None,
        "available",
    }:
        return True
    if row.get("status") == "active" and row.get("activity_known") is False:
        return True
    return bool(row.get("active"))


def _control_runtime(request: Request) -> Any | None:
    return getattr(request.app.state, "control_runtime", None)


def _bool_arg(value: Any, *, default: bool = False) -> bool:
    if value in {None, ""}:
        return default
    normalized = str(value).casefold().strip()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError("include_inactive must be a boolean")


def _session_for_machine(session_id: str, machine: str) -> AgentSession:
    session = get_tool_session_store().require_session(session_id)
    if not _belongs_to_machine(session, machine):
        raise ValueError(
            f"session {session_id} does not belong to machine {machine}"
        )
    return session


async def api_sessions(request: Request) -> Response:
    """Return coexisting final and legacy sessions with authoritative activity."""
    try:
        machine = _machine_arg(request.query_params.get("machine"))
        _require_scopes(machine)
        include_inactive = _bool_arg(
            request.query_params.get("include_inactive")
        )
        now = time.time()
        runtime = _control_runtime(request)
        final_records = (
            tuple(runtime.control_state.snapshot_sessions().values())
            if runtime is not None
            else ()
        )
        final_candidates = [
            record
            for record in final_records
            if machine == "local" or str(record.executor_id) == machine
        ]
        if machine != "local" and not final_candidates:
            require_remote_machine(machine)

        final_rows: list[dict[str, Any]] = []
        if runtime is not None and final_candidates:
            semaphore = asyncio.Semaphore(16)

            async def project_final(record: Any) -> dict[str, Any]:
                async with semaphore:
                    (
                        availability,
                        last_active_at,
                    ) = await runtime.session_coordinator.session_activity_projection(
                        str(record.session_id)
                    )
                return _final_session_payload(
                    record,
                    now=now,
                    availability=availability,
                    last_active_at=last_active_at,
                )

            final_rows = list(
                await asyncio.gather(
                    *(project_final(record) for record in final_candidates)
                )
            )

        sessions = await asyncio.to_thread(
            get_tool_session_store().list_sessions
        )
        legacy_rows = [
            _session_payload(session, now=now)
            for session in sessions
            if _belongs_to_machine(session, machine)
        ]
        rows = final_rows + legacy_rows
        if not include_inactive:
            rows = [row for row in rows if _row_visible_by_default(row)]
        rows.sort(key=_row_sort_timestamp, reverse=True)
        rows = rows[:UI_SESSION_MAX_ENTRIES]
        return _json_ok(
            {
                "machine": machine,
                "remote": machine != "local",
                "sessions": rows,
                "count": len(rows),
                "include_inactive": include_inactive,
                "active_window_hours": SESSION_ACTIVE_WINDOW_S // 3600,
            }
        )
    except HTTPException:
        raise
    except ConnectionError as exc:
        return json_error(exc, status_code=503)
    except RuntimeError as exc:
        return json_error(exc, status_code=502)
    except Exception as exc:
        return json_error(exc)


async def api_session_action(request: Request) -> Response:
    """Apply one explicit Human UI control-plane action to a session."""
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
        machine = _machine_arg(payload.get("machine"))
        session_id = bounded_text(
            payload.get("session_id"),
            field="session_id",
            max_bytes=32,
            allow_empty=False,
        )
        _require_scopes(machine, write=True)
        runtime = _control_runtime(request)
        final_record = (
            runtime.control_state.snapshot_sessions().get(session_id)
            if runtime is not None
            else None
        )
        if runtime is not None and final_record is not None:
            if machine != "local" and str(final_record.executor_id) != machine:
                raise ValueError(
                    f"session {session_id} does not belong to executor {machine}"
                )
            result = await runtime.session_coordinator.end_session(session_id)
            ended_record = runtime.control_state.snapshot_sessions().get(
                session_id
            )
            session_payload = (
                _final_session_payload(
                    ended_record,
                    now=time.time(),
                    availability="ended",
                )
                if ended_record is not None
                else {
                    "session_id": session_id,
                    "executor_id": str(final_record.executor_id),
                    "status": "ended",
                    "availability": "ended",
                    "active": False,
                    "termination_requested": True,
                }
            )
            return _json_ok(
                {
                    "machine": machine,
                    "session": session_payload,
                    "result": result,
                },
                message="Session termination completed",
            )
        _session_for_machine(session_id, machine)
        session = await asyncio.to_thread(
            get_tool_session_store().request_termination, session_id
        )
        return _json_ok(
            {
                "machine": machine,
                "session": _session_payload(session, now=time.time()),
            },
            message="Session marked for immediate termination",
        )
    except HTTPException:
        raise
    except ConnectionError as exc:
        return json_error(exc, status_code=503)
    except RuntimeError as exc:
        return json_error(exc, status_code=502)
    except Exception as exc:
        return json_error(exc)
