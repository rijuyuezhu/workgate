"""Task-first Human UI inventory over canonical task and session state."""

from typing import Any

from fastapi import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from ...oauth.core.context import MissingOAuthScopeError, require_oauth_scopes
from ...oauth.core.scopes import SCOPE_SHELL_READ
from .common import json_error
from .sessions import project_session_rows


def _json_ok(data: Any = None, message: str = "") -> JSONResponse:
    return JSONResponse({"ok": True, "message": message, "data": data})


def _require_scopes() -> None:
    try:
        require_oauth_scopes((SCOPE_SHELL_READ,))
    except MissingOAuthScopeError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


def _runtime(request: Request) -> Any:
    runtime = getattr(request.app.state, "control_runtime", None)
    if runtime is None:
        raise RuntimeError("Human UI Tasks requires the control runtime")
    return runtime


async def api_tasks(request: Request) -> Response:
    """Return retained tasks with attached retained execution sessions."""
    try:
        _require_scopes()
        runtime = _runtime(request)
        tasks = await runtime.task_service.list_tasks()
        session_rows = await project_session_rows(
            runtime,
            tuple(runtime.control_state.snapshot_sessions().values()),
        )
        by_task: dict[str, list[dict[str, Any]]] = {}
        unattached: list[dict[str, Any]] = []
        for row in session_rows:
            task_id = row.get("task_id")
            if isinstance(task_id, str) and task_id:
                by_task.setdefault(task_id, []).append(row)
            elif task_id is None:
                unattached.append(row)

        projected_tasks = []
        for task in tasks:
            task_id = str(task.task_id)
            projected_tasks.append(
                {
                    **task.model_dump(mode="json"),
                    "sessions": by_task.get(task_id, []),
                }
            )
        return _json_ok(
            {
                "tasks": projected_tasks,
                "count": len(projected_tasks),
                "unattached_sessions": unattached,
                "unattached_count": len(unattached),
            }
        )
    except HTTPException:
        raise
    except PermissionError as exc:
        return json_error(exc, status_code=403)
    except RuntimeError as exc:
        return json_error(exc, status_code=502)
    except Exception as exc:
        return json_error(exc)
