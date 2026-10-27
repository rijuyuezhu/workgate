"""Task-first WebUI inventory over canonical task and session state."""

import base64
import binascii
import json
import math
from typing import Any

from fastapi import HTTPException
from pydantic import TypeAdapter, ValidationError
from starlette.requests import Request
from starlette.responses import Response

from ...errors import PublicToolValueError
from ...oauth.core.context import MissingOAuthScopeError, require_oauth_scopes
from ...oauth.core.scopes import SCOPE_SHELL_READ, SCOPE_SHELL_WRITE
from ...protocol.ids import TaskId
from .common import json_error
from .common import json_ok as _json_ok
from .sessions import project_session_rows


def _require_scopes(*, write: bool = False) -> None:
    required = (
        (SCOPE_SHELL_READ, SCOPE_SHELL_WRITE) if write else (SCOPE_SHELL_READ,)
    )
    try:
        require_oauth_scopes(required)
    except MissingOAuthScopeError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


def _runtime(request: Request) -> Any:
    runtime = getattr(request.app.state, "control_runtime", None)
    if runtime is None:
        raise RuntimeError("WebUI Tasks requires the control runtime")
    return runtime


# Avoid projecting any non-visible Session's activity during list refresh.
_PAGE_CAP = 100
_STATUSES = frozenset(
    {"active", "blocked", "completed", "cancelled", "all", "unattached"}
)


def _sort_key(row: dict[str, Any]) -> tuple[float, float, str]:
    return (
        float(row["updated_at"]),
        float(row["created_at"]),
        str(row.get("task_id") or row["session_id"]),
    )


def _cursor_encode(status: str, row: dict[str, Any]) -> str:
    data = {"status": status, "key": _sort_key(row)}
    return (
        base64.urlsafe_b64encode(
            json.dumps(data, separators=(",", ":")).encode()
        )
        .decode()
        .rstrip("=")
    )


def _cursor_decode(cursor: str, status: str) -> tuple[float, float, str] | None:
    if not cursor:
        return None
    if len(cursor) > 512:
        raise ValueError("cursor is too long")
    try:
        data = json.loads(
            base64.b64decode(
                cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True
            )
        )
        key = data["key"]
        if (
            data["status"] != status
            or not isinstance(key, list)
            or len(key) != 3
            or not isinstance(key[2], str)
        ):
            raise ValueError("cursor does not match Task filter")
        result = (float(key[0]), float(key[1]), key[2])
        if not all(math.isfinite(n) for n in result[:2]):
            raise ValueError("cursor timestamps must be finite")
        return result
    except (
        KeyError,
        ValueError,
        TypeError,
        UnicodeDecodeError,
        binascii.Error,
    ) as exc:
        raise ValueError("invalid Task page cursor") from exc


def _page(
    rows: list[dict[str, Any]], *, status: str, limit: int, cursor: str
) -> tuple[list[dict[str, Any]], str | None]:
    after = _cursor_decode(cursor, status)
    rows.sort(key=_sort_key, reverse=True)
    if after is not None:
        rows = [row for row in rows if _sort_key(row) < after]
    visible = rows[:limit]
    next_cursor = (
        _cursor_encode(status, visible[-1]) if len(rows) > limit else None
    )
    return visible, next_cursor


async def api_tasks(request: Request) -> Response:
    """Bounded Task summaries, or a separate page of legacy unattached Sessions."""
    try:
        _require_scopes()
        runtime = _runtime(request)
        params = request.query_params
        status = params.get("status", "active")
        if status not in _STATUSES:
            raise ValueError("invalid Task status filter")
        try:
            limit = int(
                params.get("limit", str(runtime.config.ui_task_page_size))
            )
        except ValueError as exc:
            raise ValueError("limit must be an integer") from exc
        if not 1 <= limit <= _PAGE_CAP:
            raise ValueError(f"limit must be between 1 and {_PAGE_CAP}")
        cursor = params.get("cursor", "")
        # Check cursor shape/filter before touching durable state.
        _cursor_decode(cursor, status)
        records = tuple(runtime.control_state.snapshot_sessions().values())
        if status == "unattached":
            rows = [
                {
                    "session_id": str(record.session_id),
                    "created_at": float(record.created_at),
                    "updated_at": float(record.updated_at),
                    "record": record,
                }
                for record in records
                if record.task_id is None
            ]
            total = len(rows)
            page, next_cursor = _page(
                rows, status=status, limit=limit, cursor=cursor
            )
            projected = await project_session_rows(
                runtime, tuple(row["record"] for row in page)
            )
            by_id = {row["session_id"]: row for row in projected}
            # Keep visible Session order aligned with the keyset cursor, not activity.
            sessions = [by_id[row["session_id"]] for row in page]
            return _json_ok(
                {
                    "tasks": [],
                    "unattached_sessions": sessions,
                    "count": len(sessions),
                    "total": total,
                    "status": status,
                    "next_cursor": next_cursor,
                }
            )
        summaries = await runtime.task_service.list_task_summaries()
        all_count = len(summaries)
        total_active = sum(task["status"] == "active" for task in summaries)
        if status != "all":
            summaries = [task for task in summaries if task["status"] == status]
        total = len(summaries)
        page, next_cursor = _page(
            summaries, status=status, limit=limit, cursor=cursor
        )
        # Compute counts locally once. No full Session row projection.
        counts: dict[str, int] = {}
        for record in records:
            task_id = str(record.task_id or "")
            if task_id:
                counts[task_id] = counts.get(task_id, 0) + 1
        for task in page:
            task["session_count"] = counts.get(task["task_id"], 0)
        return _json_ok(
            {
                "tasks": page,
                "unattached_sessions": [],
                "count": len(page),
                "total": total,
                "active_count": total_active,
                "all_count": all_count,
                "status": status,
                "next_cursor": next_cursor,
            }
        )
    except HTTPException:
        raise
    except ValueError as exc:
        return json_error(exc, status_code=400)
    except PermissionError as exc:
        return json_error(exc, status_code=403)
    except RuntimeError as exc:
        return json_error(exc, status_code=502)
    except Exception as exc:
        return json_error(exc)


async def api_task_detail(request: Request) -> Response:
    """Expand one authorized Task's document/Sessions or one legacy Session."""
    try:
        _require_scopes()
        runtime = _runtime(request)
        task_id = request.query_params.get("task_id", "")
        session_id = request.query_params.get("session_id", "")
        if bool(task_id) == bool(session_id):
            raise ValueError("provide exactly one task_id or session_id")
        if session_id:
            record = runtime.control_state.snapshot_sessions().get(session_id)
            if record is None:
                return json_error(
                    ValueError("unknown session_id"), status_code=404
                )
            if record.task_id is None:
                projected = await project_session_rows(runtime, (record,))
                return _json_ok({"task": None, "session": projected[0]})
            task_id = str(record.task_id)
        task = await runtime.task_service.read_task(task_id)
        ids = set(task.session_ids)
        records = tuple(
            record
            for record in runtime.control_state.snapshot_sessions().values()
            if str(record.session_id) in ids
        )
        sessions = await project_session_rows(runtime, records)
        return _json_ok(
            {
                "task": {**task.model_dump(mode="json"), "sessions": sessions},
                "session": None,
            }
        )
    except HTTPException:
        raise
    except PermissionError as exc:
        return json_error(exc, status_code=403)
    except PublicToolValueError as exc:
        return json_error(
            exc,
            status_code=404 if str(exc).startswith("unknown task_id") else 400,
        )
    except ValueError as exc:
        return json_error(exc, status_code=400)
    except RuntimeError as exc:
        return json_error(exc, status_code=502)
    except Exception as exc:
        return json_error(exc)


_TASK_ID = TypeAdapter(TaskId)
_TASK_ACTIONS = {
    "block": "block_task",
    "resume": "resume_task",
    "finish": "finish_task",
    "cancel": "cancel_task",
    "delete": "delete_task",
}


async def api_task_action(request: Request) -> Response:
    """Mutate a semantic Task via its existing authenticated state machine."""
    try:
        action = str(request.path_params.get("action") or "")
        method = _TASK_ACTIONS.get(action)
        if method is None:
            return json_error(
                ValueError("unsupported Task action"), status_code=404
            )
        # Cookie-session CSRF is enforced by the shared WebUI middleware.
        _require_scopes(write=True)
        payload = await request.json()
        if not isinstance(payload, dict) or set(payload) != {"task_id"}:
            raise ValueError("Task action requires only task_id")
        try:
            task_id = str(_TASK_ID.validate_python(payload["task_id"]))
        except ValidationError as exc:
            raise ValueError("task_id must be a valid task_ id") from exc
        result = await getattr(_runtime(request).task_service, method)(task_id)
        return _json_ok(
            {
                "task_id": task_id,
                "task": result.model_dump(mode="json")
                if action != "delete"
                else None,
            },
            message=f"Task {action} completed",
        )
    except HTTPException:
        raise
    except LookupError as exc:
        return json_error(exc, status_code=404)
    except PermissionError as exc:
        return json_error(exc, status_code=403)
    except RuntimeError as exc:
        return json_error(exc, status_code=502)
    except Exception as exc:
        return json_error(exc)
