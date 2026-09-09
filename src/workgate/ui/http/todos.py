"""Authenticated Human UI APIs for control-owned shared-session Todos."""

from typing import Any

from fastapi import HTTPException
from pydantic import TypeAdapter, ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from ...config.settings import get_settings
from ...control.todos import TodoConflictError
from ...oauth.core.context import MissingOAuthScopeError, require_oauth_scopes
from ...oauth.core.scopes import (
    SCOPE_SHELL_READ,
    SCOPE_SHELL_WRITE,
)
from ...protocol.ids import SessionId
from ...schemas.result_models.todo import ReadTodosOutput, WriteTodosOutput
from .common import json_error as _json_error

UI_TODO_ID_MAX_BYTES = 256
UI_TODO_CONTENT_MAX_BYTES = 16_384
UI_TODO_LABEL_MAX_BYTES = 64
_FINAL_SESSION_ID_ADAPTER = TypeAdapter(SessionId)


def _json_ok(data: Any = None, message: str = "") -> JSONResponse:
    return JSONResponse({"ok": True, "message": message, "data": data})


def _require_scopes(*required: str) -> None:
    try:
        require_oauth_scopes(tuple(required))
    except MissingOAuthScopeError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


def _session_id_arg(value: Any) -> str:
    session_id = str(value or "").strip()
    try:
        return str(_FINAL_SESSION_ID_ADAPTER.validate_python(session_id))
    except ValidationError as exc:
        raise ValueError("session_id must be a valid shared sess_ id") from exc


def _require_todo_scopes(*, write: bool = False) -> None:
    required = [SCOPE_SHELL_READ]
    if write:
        required.append(SCOPE_SHELL_WRITE)
    _require_scopes(*required)


def _shared_session(request: Request, session_id: str) -> tuple[Any, Any]:
    runtime = getattr(request.app.state, "control_runtime", None)
    if runtime is None:
        raise RuntimeError("Human UI Todos requires the control runtime")
    record = runtime.control_state.snapshot_sessions().get(session_id)
    if record is None:
        raise LookupError(f"unknown shared session_id {session_id!r}")
    return runtime, record


def _bounded_text(
    value: Any,
    *,
    field: str,
    max_bytes: int,
    default: str,
    allow_empty: bool = True,
) -> str:
    normalized = str(value if value is not None else default)
    if not normalized and not allow_empty:
        raise ValueError(f"{field} must not be empty")
    if len(normalized.encode("utf-8")) > max_bytes:
        raise ValueError(f"{field} exceeds {max_bytes} encoded bytes")
    return normalized


def _todo_items(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        raise ValueError("todos must be a JSON array")
    settings = get_settings()
    if len(value) > settings.max_todos:
        raise ValueError(
            f"Refusing to write {len(value)} todos; max is {settings.max_todos}"
        )
    normalized: list[dict[str, str]] = []
    identifiers: set[str] = set()
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise ValueError(f"todos[{index}] must be a JSON object")
        identifier = _bounded_text(
            item.get("id"),
            field=f"todos[{index}].id",
            max_bytes=UI_TODO_ID_MAX_BYTES,
            default=str(index + 1),
            allow_empty=False,
        )
        if identifier in identifiers:
            raise ValueError(f"duplicate todo id: {identifier}")
        identifiers.add(identifier)
        normalized.append(
            {
                "id": identifier,
                "content": _bounded_text(
                    item.get("content"),
                    field=f"todos[{index}].content",
                    max_bytes=UI_TODO_CONTENT_MAX_BYTES,
                    default="",
                ),
                "status": _bounded_text(
                    item.get("status"),
                    field=f"todos[{index}].status",
                    max_bytes=UI_TODO_LABEL_MAX_BYTES,
                    default="pending",
                    allow_empty=False,
                ),
                "priority": _bounded_text(
                    item.get("priority"),
                    field=f"todos[{index}].priority",
                    max_bytes=UI_TODO_LABEL_MAX_BYTES,
                    default="medium",
                    allow_empty=False,
                ),
            }
        )
    return normalized


def _expected_revision(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("expected_revision must be a non-negative integer")
    return value


def _final_payload(
    record: Any,
    result: ReadTodosOutput | WriteTodosOutput,
) -> dict[str, Any]:
    settings = get_settings()
    session_id = str(record.session_id)
    return {
        "session_id": session_id,
        "session": {
            "session_id": session_id,
            "executor_id": str(record.executor_id),
            "workdir": record.resolved_workdir_display
            or record.requested_workdir,
            "label": record.label,
            "created_at": float(record.created_at),
            "updated_at": float(record.updated_at),
            "expires_at": None,
        },
        **result.model_dump(mode="json"),
        "limits": {
            "todos": settings.max_todos,
            "bytes": settings.max_todo_bytes,
            "id_bytes": UI_TODO_ID_MAX_BYTES,
            "content_bytes": UI_TODO_CONTENT_MAX_BYTES,
            "label_bytes": UI_TODO_LABEL_MAX_BYTES,
        },
    }


async def _read_final(runtime: Any, session_id: str) -> ReadTodosOutput:
    return await runtime.todo_service.read(session_id)


async def _write_final(
    runtime: Any,
    session_id: str,
    todos: list[dict[str, str]],
    expected_revision: int,
) -> WriteTodosOutput:
    return await runtime.todo_service.write(
        session_id,
        todos,
        expected_revision,
    )


async def api_todos(request: Request) -> Response:
    """Read or revision-guardedly replace one explicit session's todo list."""
    try:
        if request.method == "GET":
            session_id = _session_id_arg(request.query_params.get("session_id"))
            _require_todo_scopes()
            runtime, record = _shared_session(request, session_id)
            return _json_ok(
                _final_payload(record, await _read_final(runtime, session_id))
            )

        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("request body must be a JSON object")
        session_id = _session_id_arg(body.get("session_id"))
        _require_todo_scopes(write=True)
        expected_revision = _expected_revision(body.get("expected_revision"))
        todos = _todo_items(body.get("todos"))
        runtime, record = _shared_session(request, session_id)
        return _json_ok(
            _final_payload(
                record,
                await _write_final(
                    runtime,
                    session_id,
                    todos,
                    expected_revision,
                ),
            )
        )
    except HTTPException:
        raise
    except TodoConflictError as exc:
        return _json_error(exc, status_code=409)
    except LookupError as exc:
        return _json_error(exc, status_code=404)
    except ConnectionError as exc:
        return _json_error(exc, status_code=503)
    except RuntimeError as exc:
        return _json_error(exc, status_code=502)
    except Exception as exc:
        return _json_error(exc)
