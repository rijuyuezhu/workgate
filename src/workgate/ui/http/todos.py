"""Authenticated Human UI APIs for semantic-task Todos."""

from typing import Any

from fastapi import HTTPException
from pydantic import TypeAdapter, ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from ...config.control import get_control_config
from ...oauth.core.context import MissingOAuthScopeError, require_oauth_scopes
from ...oauth.core.scopes import SCOPE_SHELL_READ, SCOPE_SHELL_WRITE
from ...protocol.ids import TaskId
from ...schemas.result_models.task import TaskOutput
from ...schemas.result_models.todo import ReadTodosOutput, WriteTodosOutput
from .common import json_error as _json_error

UI_TODO_ID_MAX_BYTES = 256
UI_TODO_CONTENT_MAX_BYTES = 16_384
UI_TODO_LABEL_MAX_BYTES = 64
_FINAL_TASK_ID_ADAPTER = TypeAdapter(TaskId)


def _json_ok(data: Any = None, message: str = "") -> JSONResponse:
    return JSONResponse({"ok": True, "message": message, "data": data})


def _require_scopes(*required: str) -> None:
    try:
        require_oauth_scopes(tuple(required))
    except MissingOAuthScopeError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


def _task_id_arg(value: Any) -> str:
    task_id = str(value or "").strip()
    try:
        return str(_FINAL_TASK_ID_ADAPTER.validate_python(task_id))
    except ValidationError as exc:
        raise ValueError("task_id must be a valid task_ id") from exc


def _require_todo_scopes(*, write: bool = False) -> None:
    required = [SCOPE_SHELL_READ]
    if write:
        required.append(SCOPE_SHELL_WRITE)
    _require_scopes(*required)


def _runtime(request: Request) -> Any:
    runtime = getattr(request.app.state, "control_runtime", None)
    if runtime is None:
        raise RuntimeError("Human UI Todos requires the control runtime")
    return runtime


def _final_payload(
    task_id: str,
    result: ReadTodosOutput | WriteTodosOutput,
    *,
    task: TaskOutput,
) -> dict[str, Any]:
    settings = get_control_config()
    return {
        "task_id": task_id,
        **result.model_dump(mode="json"),
        "task": task.model_dump(mode="json"),
        "limits": {
            "todos": settings.max_todos,
            "bytes": settings.max_todo_bytes,
            "id_bytes": UI_TODO_ID_MAX_BYTES,
            "content_bytes": UI_TODO_CONTENT_MAX_BYTES,
            "label_bytes": UI_TODO_LABEL_MAX_BYTES,
        },
    }


async def api_todos(request: Request) -> Response:
    """Read or replace one explicit task's Todo projection."""
    try:
        runtime = _runtime(request)
        if request.method == "GET":
            task_id = _task_id_arg(request.query_params.get("task_id"))
            _require_todo_scopes()
            result, task = await runtime.task_service.read_with_task(task_id)
            return _json_ok(_final_payload(task_id, result, task=task))

        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("request body must be a JSON object")
        task_id = _task_id_arg(body.get("task_id"))
        _require_todo_scopes(write=True)
        todos = body.get("todos")
        if not isinstance(todos, list):
            raise ValueError("todos must be a JSON array")
        result, task = await runtime.task_service.write_with_task(
            task_id, todos
        )
        return _json_ok(_final_payload(task_id, result, task=task))
    except HTTPException:
        raise
    except LookupError as exc:
        return _json_error(exc, status_code=404)
    except PermissionError as exc:
        return _json_error(exc, status_code=403)
    except ConnectionError as exc:
        return _json_error(exc, status_code=503)
    except RuntimeError as exc:
        return _json_error(exc, status_code=502)
    except Exception as exc:
        return _json_error(exc)
