"""Task-first WebUI inventory over canonical task and session state."""

from typing import Any

from fastapi import HTTPException
from pydantic import TypeAdapter, ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from ...oauth.core.context import MissingOAuthScopeError, require_oauth_scopes
from ...oauth.core.scopes import SCOPE_SHELL_READ, SCOPE_SHELL_WRITE
from ...protocol.ids import TaskId
from .common import json_error
from .sessions import project_session_rows


def _json_ok(data: Any = None, message: str = "") -> JSONResponse:
    return JSONResponse({"ok": True, "message": message, "data": data})


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
