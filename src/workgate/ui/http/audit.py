"""Authenticated Human UI APIs for local and remote Audit records."""

import asyncio
import base64
import binascii
from typing import Any, cast

from fastapi import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from ...audit import (
    audit_query_snapshot,
    get_audit_entry,
    get_session_audit_entry,
    query_audit,
    query_session_audit,
    summarize_audit_entry,
)
from ...config.settings import get_settings
from ...oauth.core.context import MissingOAuthScopeError, require_oauth_scopes
from ...oauth.core.scopes import (
    SCOPE_AUDIT_FULL,
    SCOPE_AUDIT_READ,
    SCOPE_FILE_SHARE,
    SCOPE_GIT_WRITE,
    SCOPE_REMOTE_USE,
    SCOPE_SHELL_EXECUTE,
    SCOPE_SHELL_WRITE,
    SUPPORTED_OAUTH_SCOPES,
)
from ...ops.image import detect_image_type
from ...remote.service import call_remote_worker_tool
from ...tool_session.store import get_tool_session_store
from .common import (
    bounded_int as _bounded_int,
)
from .common import (
    bounded_text as _bounded_text,
)
from .common import (
    json_error as _json_error,
)
from .common import (
    require_remote_machine as _require_remote_machine,
)
from .image_preview import (
    UiImagePreviewRequest,
    image_preview_request,
    terminal_image_fields,
)

UI_AUDIT_MACHINE_MAX_BYTES = 255
UI_AUDIT_ENTRY_ID_MAX_BYTES = 512
UI_AUDIT_FILTER_MAX_BYTES = 1_024
UI_AUDIT_SEARCH_MAX_BYTES = 4_096
UI_AUDIT_MAX_ENTRIES = 2_000
UI_AUDIT_REMOTE_TIMEOUT_S = 60

_AUDIT_FILE_WRITE_TOOLS = frozenset(
    {
        "apply_patch",
        "delete_file_or_dir",
        "edit_lines",
        "hashline_edit",
        "write_file",
    }
)
_AUDIT_EXECUTE_TOOLS = frozenset(
    {
        "bash",
        "job",
        "kill_persistent_shell",
        "resize_persistent_shell",
        "run_python_code",
        "send_persistent_shell_input",
        "session_start",
    }
)


def _json_ok(data: Any = None, message: str = "") -> JSONResponse:
    return JSONResponse({"ok": True, "message": message, "data": data})


def _require_scopes(*required: str) -> None:
    try:
        require_oauth_scopes(tuple(dict.fromkeys(required)))
    except MissingOAuthScopeError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


def _machine_arg(value: Any) -> str:
    return _bounded_text(
        value,
        field="machine",
        max_bytes=UI_AUDIT_MACHINE_MAX_BYTES,
        default="local",
        allow_empty=False,
    )


def _bounded_float(value: Any, *, field: str) -> float | None:
    if value in {None, ""}:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a number")
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a number") from exc


def _sort_arg(value: Any) -> str:
    normalized = _bounded_text(
        value,
        field="sort",
        max_bytes=16,
        default="desc",
        allow_empty=False,
    ).casefold()
    if normalized not in {"asc", "desc"}:
        raise ValueError("sort must be asc or desc")
    return normalized


def _scope_arg(value: Any) -> str:
    normalized = _bounded_text(
        value,
        field="scope",
        max_bytes=16,
        default="global",
        allow_empty=False,
    ).casefold()
    if normalized not in {"global", "session"}:
        raise ValueError("scope must be global or session")
    return normalized


def _bool_arg(value: Any, *, default: bool = False) -> bool:
    if value in {None, ""}:
        return default
    normalized = str(value).casefold().strip()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError("boolean query parameter is invalid")


def _query_args(request: Request) -> dict[str, Any]:
    params = request.query_params
    start_ts = _bounded_float(params.get("start_ts"), field="start_ts")
    end_ts = _bounded_float(params.get("end_ts"), field="end_ts")
    if start_ts is not None and end_ts is not None and start_ts > end_ts:
        raise ValueError("start_ts must not be greater than end_ts")
    return {
        "limit": _bounded_int(
            params.get("limit"),
            field="limit",
            default=300,
            minimum=1,
            maximum=UI_AUDIT_MAX_ENTRIES,
        ),
        "event": _bounded_text(
            params.get("event"),
            field="event",
            max_bytes=UI_AUDIT_FILTER_MAX_BYTES,
        ),
        "operation": _bounded_text(
            params.get("operation"),
            field="operation",
            max_bytes=UI_AUDIT_FILTER_MAX_BYTES,
        ),
        "session": _bounded_text(
            params.get("session"),
            field="session",
            max_bytes=UI_AUDIT_FILTER_MAX_BYTES,
        ),
        "search": _bounded_text(
            params.get("search"),
            field="search",
            max_bytes=UI_AUDIT_SEARCH_MAX_BYTES,
        ),
        "start_ts": start_ts,
        "end_ts": end_ts,
        "sort": _sort_arg(params.get("sort")),
    }


def _remote_timeout_s() -> int:
    return max(
        1,
        min(
            int(get_settings().remote_job_timeout_s),
            UI_AUDIT_REMOTE_TIMEOUT_S,
        ),
    )


def _remote_result_data(
    result: dict[str, Any], *, machine: str, tool: str
) -> dict[str, Any]:
    if not result.get("ok", False):
        raise RuntimeError(
            str(result.get("message") or f"remote {tool} failed on {machine}")
        )
    data = result.get("data")
    if isinstance(data, dict) and data.get("status") == "error":
        error_type = str(data.get("error_type") or "remote_error")
        message = str(
            data.get("message") or f"remote {tool} failed on {machine}"
        )
        raise RuntimeError(f"{error_type}: {message}")
    if not isinstance(data, dict):
        raise RuntimeError(
            f"Remote machine {machine} returned malformed audit data"
        )
    return cast(dict[str, Any], data)


async def _remote_audit_call(
    machine: str, tool: str, args: dict[str, Any]
) -> dict[str, Any]:
    _require_remote_machine(machine)
    result = await call_remote_worker_tool(
        machine,
        tool,
        args,
        _remote_timeout_s(),
    )
    return _remote_result_data(result, machine=machine, tool=tool)


def _remote_session_projection(machine: str) -> dict[str, str]:
    """Build one worker-to-public session map for a remote Audit response."""
    return {
        session.worker_session_id: session.session_id
        for session in get_tool_session_store().list_sessions()
        if session.target == "remote"
        and session.machine == machine
        and session.worker_session_id
    }


def _final_session_record(
    request: Request, machine: str, session_id: str
) -> Any | None:
    if not session_id:
        return None
    runtime = getattr(request.app.state, "control_runtime", None)
    if runtime is None:
        return None
    record = runtime.control_state.snapshot_sessions().get(session_id)
    if record is None:
        return None
    if machine != "local" and str(record.executor_id) != machine:
        raise ValueError(
            f"session {session_id} does not belong to executor {machine}"
        )
    return record


def _normalize_entry(
    machine: str,
    value: Any,
    *,
    session_projection: dict[str, str] | None = None,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RuntimeError(
            f"Machine {machine} returned a malformed audit entry"
        )
    entry = cast(dict[str, Any], dict(value))
    identifier = _bounded_text(
        entry.get("id"),
        field="audit entry id",
        max_bytes=UI_AUDIT_ENTRY_ID_MAX_BYTES,
        allow_empty=False,
    )
    entry["id"] = identifier
    entry["node"] = machine
    try:
        entry["ts"] = float(entry.get("ts") or 0)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            f"Machine {machine} returned a malformed audit timestamp"
        ) from exc
    entry["event"] = str(entry.get("event") or "unknown")
    entry["operation"] = str(entry.get("operation") or "other")
    if "tool" in entry:
        entry["tool"] = str(entry.get("tool") or "unknown")
    if session_projection is not None and entry.get("session"):
        worker_session_id = str(entry["session"])
        entry["session"] = session_projection.get(
            worker_session_id, worker_session_id
        )
    return entry


def _remote_query_args(machine: str, args: dict[str, Any]) -> dict[str, Any]:
    """Translate a public control-session filter to its worker-side session id."""
    public_session_id = str(args.get("session") or "")
    if not public_session_id:
        return args
    session = get_tool_session_store().require_session(public_session_id)
    if session.target != "remote" or session.machine != machine:
        raise ValueError(
            f"session {public_session_id} does not belong to machine {machine}"
        )
    if not session.worker_session_id:
        raise RuntimeError(
            f"remote session {public_session_id} is missing its worker binding"
        )
    return {**args, "session": session.worker_session_id}


def _audit_view_image_detail(
    entry: dict[str, Any],
    preview_request: UiImagePreviewRequest | None = None,
) -> dict[str, Any]:
    """Sanitize an audited MCP image result and attach bounded UI previews."""
    if str(entry.get("tool") or "") != "view_image":
        return entry
    output = entry.get("output")
    if not isinstance(output, dict):
        return entry
    content = output.get("content")
    if not isinstance(content, list):
        return entry
    image_index = next(
        (
            index
            for index, item in enumerate(content)
            if isinstance(item, dict)
            and item.get("type") == "image"
            and isinstance(item.get("data"), str)
        ),
        None,
    )
    if image_index is None:
        return entry

    source_item = content[image_index]
    assert isinstance(source_item, dict)
    sanitized_item = {
        name: value for name, value in source_item.items() if name != "data"
    }
    sanitized_content = list(content)
    sanitized_content[image_index] = sanitized_item
    detail = {**entry, "output": {**output, "content": sanitized_content}}
    try:
        raw = base64.b64decode(str(source_item["data"]), validate=True)
        maximum = max(1, int(get_settings().max_view_image_bytes))
        if not raw:
            raise ValueError("Image payload is empty")
        if len(raw) > maximum:
            raise ValueError(
                f"Refusing image of {len(raw)} bytes; max is {maximum}"
            )
        _, mime_type = detect_image_type(raw[:16])
        structured = output.get("structuredContent")
        if not isinstance(structured, dict):
            structured = output.get("structured_content")
        path = (
            str(structured.get("path") or "image result")
            if isinstance(structured, dict)
            else "image result"
        )
        sanitized_item["bytes"] = len(raw)
        detail["image_preview"] = {
            "kind": "image",
            "path": path,
            "bytes": len(raw),
            "mime_type": mime_type,
            "data_base64": base64.b64encode(raw).decode("ascii"),
            **terminal_image_fields(raw, preview_request),
        }
    except (ValueError, OSError, binascii.Error) as exc:
        detail["image_preview_error"] = str(exc)
    return detail


def _summary_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """Return metadata-only list data; protected payloads require detail scopes."""
    return summarize_audit_entry(entry)


def _normalize_query_result(
    machine: str,
    value: Any,
    *,
    session_projection: dict[str, str] | None = None,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RuntimeError(f"Machine {machine} returned malformed audit data")
    rows = value.get("entries")
    if not isinstance(rows, list):
        raise RuntimeError(
            f"Machine {machine} returned malformed audit entries"
        )
    if len(rows) > UI_AUDIT_MAX_ENTRIES:
        raise RuntimeError(f"Machine {machine} returned too many audit entries")
    entries = [
        _normalize_entry(
            machine,
            item,
            session_projection=session_projection,
        )
        for item in rows
    ]
    count = value.get("count", len(entries))
    total_matched = value.get("total_matched", count)
    if (
        isinstance(count, bool)
        or not isinstance(count, int)
        or isinstance(total_matched, bool)
        or not isinstance(total_matched, int)
        or count < 0
        or total_matched < count
    ):
        raise RuntimeError(f"Machine {machine} returned malformed audit counts")
    if count != len(entries):
        raise RuntimeError(
            f"Machine {machine} returned inconsistent audit counts"
        )
    return {
        "entries": [_summary_entry(entry) for entry in entries],
        "count": count,
        "total_matched": total_matched,
    }


async def _query(
    machine: str,
    args: dict[str, Any],
    *,
    log_session_id: str | None = None,
    final_session: bool = False,
    include_selected: bool = False,
    selected_id: str = "",
) -> dict[str, Any]:
    query_args = dict(args)
    if log_session_id:
        query_args.pop("session", None)
    if final_session:
        assert log_session_id
        value = await asyncio.to_thread(
            query_audit, **{**query_args, "session": log_session_id}
        )
        if include_selected:
            value = audit_query_snapshot(value, selected_id=selected_id)
    elif machine == "local":
        if log_session_id:
            value = await asyncio.to_thread(
                query_session_audit, log_session_id, **query_args
            )
        else:
            value = await asyncio.to_thread(query_audit, **query_args)
        if include_selected:
            value = audit_query_snapshot(value, selected_id=selected_id)
    else:
        remote_args = dict(query_args)
        if log_session_id:
            translated = _remote_query_args(
                machine, {"session": log_session_id}
            )
            worker_session_id = str(translated.get("session") or "")
            if not worker_session_id:
                raise RuntimeError(
                    f"remote session {log_session_id} is missing its worker binding"
                )
            remote_args["log_session_id"] = worker_session_id
        else:
            remote_args = _remote_query_args(machine, remote_args)
        if include_selected:
            remote_args["snapshot"] = True
            remote_args["selected_id"] = selected_id
        else:
            remote_args["summary_only"] = True
        value = await _remote_audit_call(machine, "query_audit", remote_args)
    projection = (
        _remote_session_projection(machine)
        if machine != "local" and not log_session_id
        else None
    )
    result = _normalize_query_result(
        machine,
        value,
        session_projection=projection,
    )
    if log_session_id:
        for entry in result["entries"]:
            entry["session"] = log_session_id
    selected = value.get("entry") if isinstance(value, dict) else None
    if selected is not None:
        detail = _normalize_entry(
            machine,
            selected,
            session_projection=projection,
        )
        if log_session_id:
            detail["session"] = log_session_id
        result["entry"] = detail
    return result


async def _detail(
    machine: str,
    entry_id: str,
    *,
    include_full_payloads: bool = False,
    log_session_id: str | None = None,
    final_session: bool = False,
) -> dict[str, Any]:
    if final_session:
        assert log_session_id
        value = await asyncio.to_thread(
            get_audit_entry,
            entry_id,
            include_full_payloads=include_full_payloads,
        )
        if str(value.get("session") or "") != log_session_id:
            raise ValueError(f"Unknown audit entry: {entry_id}")
    elif machine == "local":
        if log_session_id:
            value = await asyncio.to_thread(
                get_session_audit_entry,
                log_session_id,
                entry_id,
                include_full_payloads=include_full_payloads,
            )
        else:
            value = await asyncio.to_thread(
                get_audit_entry,
                entry_id,
                include_full_payloads=include_full_payloads,
            )
    else:
        args: dict[str, Any] = {
            "id": entry_id,
            "include_full_payloads": include_full_payloads,
        }
        if log_session_id:
            translated = _remote_query_args(
                machine, {"session": log_session_id}
            )
            args["log_session_id"] = translated["session"]
        value = await _remote_audit_call(
            machine,
            "get_audit_entry",
            args,
        )
    projection = (
        _remote_session_projection(machine)
        if machine != "local" and not log_session_id
        else None
    )
    detail = _normalize_entry(
        machine,
        value,
        session_projection=projection,
    )
    if log_session_id:
        detail["session"] = log_session_id
    return detail


def _detail_scopes(machine: str, entry: dict[str, Any]) -> tuple[str, ...]:
    required = {SCOPE_AUDIT_READ}
    tool = str(entry.get("tool") or "")
    operation = str(entry.get("operation") or "").casefold()
    if tool in _AUDIT_FILE_WRITE_TOOLS:
        required.add(SCOPE_SHELL_WRITE)
    if tool in _AUDIT_EXECUTE_TOOLS or operation in {"shell", "jobs"}:
        required.add(SCOPE_SHELL_EXECUTE)
    if tool.startswith("git_"):
        required.add(SCOPE_GIT_WRITE)
    if operation == "transfer":
        required.add(SCOPE_FILE_SHARE)
    if machine != "local" or operation == "remote":
        required.add(SCOPE_REMOTE_USE)
    if operation == "other":
        required.update(
            scope
            for scope in SUPPORTED_OAUTH_SCOPES
            if scope != SCOPE_AUDIT_FULL
        )
    return tuple(scope for scope in SUPPORTED_OAUTH_SCOPES if scope in required)


def _payload(
    machine: str, data: dict[str, Any], *, scope: str = "global"
) -> dict[str, Any]:
    return {
        "machine": machine,
        "remote": machine != "local",
        "scope": scope,
        **data,
        "limits": {
            "entries": UI_AUDIT_MAX_ENTRIES,
            "filter_bytes": UI_AUDIT_FILTER_MAX_BYTES,
            "search_bytes": UI_AUDIT_SEARCH_MAX_BYTES,
        },
    }


async def api_audit(request: Request) -> Response:
    """Return a bounded filtered list from one machine's private Audit log."""
    try:
        machine = _machine_arg(request.query_params.get("machine"))
        required = [SCOPE_AUDIT_READ]
        if machine != "local":
            required.append(SCOPE_REMOTE_USE)
        _require_scopes(*required)
        scope = _scope_arg(request.query_params.get("scope"))
        args = _query_args(request)
        log_session_id = str(args.get("session") or "")
        if scope == "session" and not log_session_id:
            raise ValueError("session is required when scope=session")
        final_session = bool(
            scope == "session"
            and _final_session_record(request, machine, log_session_id)
            is not None
        )
        include_selected = _bool_arg(
            request.query_params.get("include_selected")
        )
        selected_id = _bounded_text(
            request.query_params.get("selected_id"),
            field="selected_id",
            max_bytes=UI_AUDIT_ENTRY_ID_MAX_BYTES,
        )
        result = await _query(
            machine,
            args,
            log_session_id=(log_session_id if scope == "session" else None),
            final_session=final_session,
            include_selected=include_selected,
            selected_id=selected_id,
        )
        entry = result.get("entry")
        if isinstance(entry, dict):
            try:
                _require_scopes(*_detail_scopes(machine, entry))
            except HTTPException as exc:
                result.pop("entry", None)
                result["entry_error"] = str(exc.detail)
            else:
                result["entry"] = await asyncio.to_thread(
                    _audit_view_image_detail,
                    entry,
                    image_preview_request(request.query_params),
                )
        return _json_ok(
            _payload(
                machine,
                result,
                scope=scope,
            )
        )
    except HTTPException:
        raise
    except ConnectionError as exc:
        return _json_error(exc, status_code=503)
    except RuntimeError as exc:
        return _json_error(exc, status_code=502)
    except Exception as exc:
        return _json_error(exc)


async def api_audit_detail(request: Request) -> Response:
    """Return one audit entry after enforcing operation-sensitive scopes."""
    try:
        machine = _machine_arg(request.query_params.get("machine"))
        entry_id = _bounded_text(
            request.query_params.get("id"),
            field="id",
            max_bytes=UI_AUDIT_ENTRY_ID_MAX_BYTES,
            allow_empty=False,
        )
        base_scopes = [SCOPE_AUDIT_READ]
        if machine != "local":
            base_scopes.append(SCOPE_REMOTE_USE)
        _require_scopes(*base_scopes)
        scope = _scope_arg(request.query_params.get("scope"))
        log_session_id = _bounded_text(
            request.query_params.get("session"),
            field="session",
            max_bytes=UI_AUDIT_FILTER_MAX_BYTES,
        )
        if scope == "session" and not log_session_id:
            raise ValueError("session is required when scope=session")
        final_session = bool(
            scope == "session"
            and _final_session_record(request, machine, log_session_id)
            is not None
        )
        entry = await _detail(
            machine,
            entry_id,
            log_session_id=(log_session_id if scope == "session" else None),
            final_session=final_session,
        )
        _require_scopes(*_detail_scopes(machine, entry))
        include_full_payloads = str(
            request.query_params.get("include_full_payloads") or ""
        ).casefold() in {"1", "true", "yes"}
        if include_full_payloads:
            _require_scopes(SCOPE_AUDIT_READ, SCOPE_AUDIT_FULL)
            entry = await _detail(
                machine,
                entry_id,
                include_full_payloads=True,
                log_session_id=(log_session_id if scope == "session" else None),
                final_session=final_session,
            )
        preview_request = image_preview_request(request.query_params)
        entry = await asyncio.to_thread(
            _audit_view_image_detail, entry, preview_request
        )
        return _json_ok(_payload(machine, {"entry": entry}, scope=scope))
    except HTTPException:
        raise
    except ValueError as exc:
        return _json_error(exc, status_code=404)
    except ConnectionError as exc:
        return _json_error(exc, status_code=503)
    except RuntimeError as exc:
        return _json_error(exc, status_code=502)
    except Exception as exc:
        return _json_error(exc)
