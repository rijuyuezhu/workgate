"""Authenticated Human UI inventory for control-owned shared sessions."""

import asyncio
import time
from typing import Any

from fastapi import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from ...oauth.core.context import MissingOAuthScopeError, require_oauth_scopes
from ...oauth.core.scopes import SCOPE_SHELL_READ, SCOPE_SHELL_WRITE
from .common import bounded_text, json_error

UI_SESSION_EXECUTOR_MAX_BYTES = 128
UI_SESSION_MAX_ENTRIES = 2_000
UI_SESSION_ACTIVE_WINDOW_S = 5 * 60 * 60


def _json_ok(data: Any = None, message: str = "") -> JSONResponse:
    return JSONResponse({"ok": True, "message": message, "data": data})


def _executor_arg(value: Any) -> str | None:
    if value in {None, ""}:
        return None
    return bounded_text(
        value,
        field="executor_id",
        max_bytes=UI_SESSION_EXECUTOR_MAX_BYTES,
        allow_empty=False,
    )


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
        and last_active_at >= now - UI_SESSION_ACTIVE_WINDOW_S,
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


def _control_runtime(request: Request) -> Any:
    runtime = getattr(request.app.state, "control_runtime", None)
    if runtime is None:
        raise RuntimeError("Human UI Sessions requires the control runtime")
    return runtime


def _bool_arg(value: Any, *, default: bool = False) -> bool:
    if value in {None, ""}:
        return default
    normalized = str(value).casefold().strip()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError("include_inactive must be a boolean")


async def api_sessions(request: Request) -> Response:
    """Return canonical shared sessions with authoritative activity projection."""
    try:
        _require_scopes()
        executor_id = _executor_arg(request.query_params.get("executor_id"))
        include_inactive = _bool_arg(
            request.query_params.get("include_inactive")
        )
        now = time.time()
        runtime = _control_runtime(request)
        records = tuple(runtime.control_state.snapshot_sessions().values())
        candidates = [
            record
            for record in records
            if executor_id is None or str(record.executor_id) == executor_id
        ]

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
                now=now,
                availability=availability,
                last_active_at=last_active_at,
            )

        rows = list(
            await asyncio.gather(*(project(record) for record in candidates))
        )
        if not include_inactive:
            rows = [row for row in rows if _row_visible_by_default(row)]
        rows.sort(key=_row_sort_timestamp, reverse=True)
        rows = rows[:UI_SESSION_MAX_ENTRIES]
        return _json_ok(
            {
                "executor_id": executor_id,
                "sessions": rows,
                "count": len(rows),
                "include_inactive": include_inactive,
                "active_window_hours": UI_SESSION_ACTIVE_WINDOW_S // 3600,
            }
        )
    except HTTPException:
        raise
    except ValueError as exc:
        return json_error(exc, status_code=400)
    except RuntimeError as exc:
        return json_error(exc, status_code=502)
    except Exception as exc:
        return json_error(exc)


async def api_session_action(request: Request) -> Response:
    """Apply one explicit control-plane action to a shared session."""
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
                ValueError(f"unknown shared session_id {session_id!r}"),
                status_code=404,
            )
        result = await runtime.session_coordinator.end_session(session_id)
        ended_record = runtime.control_state.snapshot_sessions().get(session_id)
        session_payload = (
            _final_session_payload(
                ended_record,
                now=time.time(),
                availability="ended",
            )
            if ended_record is not None
            else {
                "session_id": session_id,
                "executor_id": str(record.executor_id),
                "status": "ended",
                "availability": "ended",
                "active": False,
                "termination_requested": True,
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
