"""Authenticated Human UI APIs for control-owned Audit records."""

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
    query_audit,
    summarize_audit_entry,
)
from ...config.settings import get_settings
from ...oauth.core.context import MissingOAuthScopeError, require_oauth_scopes
from ...oauth.core.scopes import (
    SCOPE_AUDIT_FULL,
    SCOPE_AUDIT_READ,
    SCOPE_FILE_SHARE,
    SCOPE_GIT_WRITE,
    SCOPE_SHELL_EXECUTE,
    SCOPE_SHELL_WRITE,
    SUPPORTED_OAUTH_SCOPES,
)
from ...utils.image_types import detect_image_type
from .common import (
    bounded_int as _bounded_int,
)
from .common import (
    bounded_text as _bounded_text,
)
from .common import (
    json_error as _json_error,
)
from .image_preview import (
    UiImagePreviewRequest,
    image_preview_request,
    terminal_image_fields,
)

UI_AUDIT_ENTRY_ID_MAX_BYTES = 512
UI_AUDIT_FILTER_MAX_BYTES = 1_024
UI_AUDIT_SEARCH_MAX_BYTES = 4_096
UI_AUDIT_MAX_ENTRIES = 2_000

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


def _final_session_record(request: Request, session_id: str) -> Any | None:
    if not session_id:
        return None
    runtime = getattr(request.app.state, "control_runtime", None)
    if runtime is None:
        return None
    record = runtime.control_state.snapshot_sessions().get(session_id)
    if record is None:
        return None
    return record


def _normalize_entry(node: str, value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RuntimeError(
            f"Audit node {node} returned a malformed audit entry"
        )
    entry = cast(dict[str, Any], dict(value))
    identifier = _bounded_text(
        entry.get("id"),
        field="audit entry id",
        max_bytes=UI_AUDIT_ENTRY_ID_MAX_BYTES,
        allow_empty=False,
    )
    entry["id"] = identifier
    entry["node"] = node
    try:
        entry["ts"] = float(entry.get("ts") or 0)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            f"Audit node {node} returned a malformed audit timestamp"
        ) from exc
    entry["event"] = str(entry.get("event") or "unknown")
    entry["operation"] = str(entry.get("operation") or "other")
    if "tool" in entry:
        entry["tool"] = str(entry.get("tool") or "unknown")
    return entry


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


def _normalize_query_result(node: str, value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RuntimeError(f"Audit node {node} returned malformed audit data")
    rows = value.get("entries")
    if not isinstance(rows, list):
        raise RuntimeError(
            f"Audit node {node} returned malformed audit entries"
        )
    if len(rows) > UI_AUDIT_MAX_ENTRIES:
        raise RuntimeError(f"Audit node {node} returned too many audit entries")
    entries = [_normalize_entry(node, item) for item in rows]
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
        raise RuntimeError(f"Audit node {node} returned malformed audit counts")
    if count != len(entries):
        raise RuntimeError(
            f"Audit node {node} returned inconsistent audit counts"
        )
    return {
        "entries": [_summary_entry(entry) for entry in entries],
        "count": count,
        "total_matched": total_matched,
    }


async def _query(
    node: str,
    args: dict[str, Any],
    *,
    log_session_id: str | None = None,
    include_selected: bool = False,
    selected_id: str = "",
) -> dict[str, Any]:
    query_args = dict(args)
    if log_session_id:
        query_args.pop("session", None)
    value = await asyncio.to_thread(
        query_audit,
        **(
            {**query_args, "session": log_session_id}
            if log_session_id
            else query_args
        ),
    )
    if include_selected:
        value = audit_query_snapshot(value, selected_id=selected_id)
    result = _normalize_query_result(
        node,
        value,
    )
    if log_session_id:
        for entry in result["entries"]:
            entry["session"] = log_session_id
    selected = value.get("entry") if isinstance(value, dict) else None
    if selected is not None:
        detail = _normalize_entry(
            node,
            selected,
        )
        if log_session_id:
            detail["session"] = log_session_id
        result["entry"] = detail
    return result


async def _detail(
    node: str,
    entry_id: str,
    *,
    include_full_payloads: bool = False,
    log_session_id: str | None = None,
) -> dict[str, Any]:
    value = await asyncio.to_thread(
        get_audit_entry,
        entry_id,
        include_full_payloads=include_full_payloads,
    )
    if log_session_id and str(value.get("session") or "") != log_session_id:
        raise ValueError(f"Unknown audit entry: {entry_id}")
    detail = _normalize_entry(
        node,
        value,
    )
    if log_session_id:
        detail["session"] = log_session_id
    return detail


def _detail_scopes(entry: dict[str, Any]) -> tuple[str, ...]:
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
    if operation == "other":
        required.update(
            scope
            for scope in SUPPORTED_OAUTH_SCOPES
            if scope != SCOPE_AUDIT_FULL
        )
    return tuple(scope for scope in SUPPORTED_OAUTH_SCOPES if scope in required)


def _payload(data: dict[str, Any], *, scope: str = "global") -> dict[str, Any]:
    return {
        "scope": scope,
        **data,
        "limits": {
            "entries": UI_AUDIT_MAX_ENTRIES,
            "filter_bytes": UI_AUDIT_FILTER_MAX_BYTES,
            "search_bytes": UI_AUDIT_SEARCH_MAX_BYTES,
        },
    }


async def api_audit(request: Request) -> Response:
    """Return a bounded filtered list from control-owned Audit history."""
    try:
        _require_scopes(SCOPE_AUDIT_READ)
        scope = _scope_arg(request.query_params.get("scope"))
        args = _query_args(request)
        log_session_id = str(args.get("session") or "")
        if scope == "session":
            if not log_session_id:
                raise ValueError("session is required when scope=session")
            if _final_session_record(request, log_session_id) is None:
                raise LookupError(
                    f"unknown shared session_id {log_session_id!r}"
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
            "control",
            args,
            log_session_id=(log_session_id if scope == "session" else None),
            include_selected=include_selected,
            selected_id=selected_id,
        )
        entry = result.get("entry")
        if isinstance(entry, dict):
            try:
                _require_scopes(*_detail_scopes(entry))
            except HTTPException as exc:
                result.pop("entry", None)
                result["entry_error"] = str(exc.detail)
            else:
                result["entry"] = await asyncio.to_thread(
                    _audit_view_image_detail,
                    entry,
                    image_preview_request(request.query_params),
                )
        return _json_ok(_payload(result, scope=scope))
    except HTTPException:
        raise
    except LookupError as exc:
        return _json_error(exc, status_code=404)
    except ValueError as exc:
        return _json_error(exc, status_code=400)
    except RuntimeError as exc:
        return _json_error(exc, status_code=502)
    except Exception as exc:
        return _json_error(exc)


async def api_audit_detail(request: Request) -> Response:
    """Return one control audit entry after operation-sensitive scope checks."""
    try:
        _require_scopes(SCOPE_AUDIT_READ)
        entry_id = _bounded_text(
            request.query_params.get("id"),
            field="id",
            max_bytes=UI_AUDIT_ENTRY_ID_MAX_BYTES,
            allow_empty=False,
        )
        scope = _scope_arg(request.query_params.get("scope"))
        log_session_id = _bounded_text(
            request.query_params.get("session"),
            field="session",
            max_bytes=UI_AUDIT_FILTER_MAX_BYTES,
        )
        if scope == "session":
            if not log_session_id:
                raise ValueError("session is required when scope=session")
            if _final_session_record(request, log_session_id) is None:
                raise LookupError(
                    f"unknown shared session_id {log_session_id!r}"
                )
        scoped_session_id = log_session_id if scope == "session" else None
        entry = await _detail(
            "control",
            entry_id,
            log_session_id=scoped_session_id,
        )
        _require_scopes(*_detail_scopes(entry))
        include_full_payloads = str(
            request.query_params.get("include_full_payloads") or ""
        ).casefold() in {"1", "true", "yes"}
        if include_full_payloads:
            _require_scopes(SCOPE_AUDIT_READ, SCOPE_AUDIT_FULL)
            entry = await _detail(
                "control",
                entry_id,
                include_full_payloads=True,
                log_session_id=scoped_session_id,
            )
        preview_request = image_preview_request(request.query_params)
        entry = await asyncio.to_thread(
            _audit_view_image_detail, entry, preview_request
        )
        return _json_ok(_payload({"entry": entry}, scope=scope))
    except HTTPException:
        raise
    except LookupError as exc:
        return _json_error(exc, status_code=404)
    except ValueError as exc:
        return _json_error(exc, status_code=404)
    except RuntimeError as exc:
        return _json_error(exc, status_code=502)
    except Exception as exc:
        return _json_error(exc)
