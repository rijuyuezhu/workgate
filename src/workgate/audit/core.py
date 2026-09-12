"""Append redacted, portable, bounded audit events to a private JSONL log."""

import contextlib
import hashlib
import json
import math
import re
import threading
import time
import uuid
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import fields, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from ..agent_bridge.redaction import (
    SENSITIVE_FLAG_RE,
    SENSITIVE_KEY_RE,
    _redact_text,
    redact_configured_values,
)
from ..config.settings import Settings, get_settings
from ..persistence import get_state_store
from ..tools.session_args import tool_input_session_ids
from ..utils.private_files import (
    append_private_bytes,
    atomic_write_private_bytes,
    private_file_lock,
)
from .payloads import (
    externalize_sanitized_value,
    payload_file_sizes,
    payload_reference_digests,
    payload_reference_metadata,
    prune_payload_files,
    resolve_payload_references,
)

DOWNLOAD_URL_TOKEN_RE = re.compile(
    r"(?P<prefix>(?:https?://[^\s/?#]+)?/download/)[A-Za-z0-9_-]+"
)

_AUDIT_THREAD_LOCK = threading.RLock()
_CURRENT_AUDIT_CALL_ID: ContextVar[str | None] = ContextVar(
    "workgate_audit_call_id", default=None
)
_CURRENT_AUDIT_SESSION_IDS: ContextVar[tuple[str, ...]] = ContextVar(
    "workgate_audit_session_ids", default=()
)
_AUDIT_PAYLOAD_SWEEP_INTERVAL_S = 60.0
_AUDIT_PAYLOAD_SWEEP_TIMES: dict[str, float] = {}
_AUDIT_MAX_DEPTH = 12
_AUDIT_MAX_ITEMS = 200
_AUDIT_MAX_STRING_CHARS = 16_000
_AUDIT_PREVIEW_DEPTH = 4
_AUDIT_PREVIEW_ITEMS = 20
_AUDIT_PREVIEW_STRING_CHARS = 512
_AUDIT_TRUNCATED_KEY = "$workgate_audit_truncated"
_AUDIT_BINARY_KEY = "$workgate_audit_binary"
_AUDIT_CYCLE_KEY = "$workgate_audit_cycle"
_AUDIT_SAFE_IDENTIFIER_SUFFIXES = ("_sha256", "_fingerprint", "_token_id")


def _audit_key_is_sensitive(name: str) -> bool:
    """Treat credential values as sensitive while retaining irreversible identifiers."""
    normalized = name.casefold()
    if normalized.endswith(_AUDIT_SAFE_IDENTIFIER_SUFFIXES):
        return False
    return bool(SENSITIVE_KEY_RE.search(name))


def _configured_secret_maps() -> tuple[dict[str, str], ...]:
    """Return configured secrets that must be removed wherever rendered."""
    settings = get_settings()
    secrets: dict[str, str] = {}
    if settings.oauth_admin_pin:
        secrets["oauth_admin_pin"] = settings.oauth_admin_pin
    return (secrets,) if secrets else ()


def _redact_download_urls(value: str) -> str:
    """Mask tokenized download URL path segments in audit text."""
    return DOWNLOAD_URL_TOKEN_RE.sub(r"\g<prefix><redacted>", value)


def _bounded_text(value: str, max_chars: int) -> str:
    """Redact one free-form string and bound its retained character count."""
    redacted = redact_configured_values(value, *_configured_secret_maps())
    redacted = _redact_download_urls(_redact_text(redacted))
    if len(redacted) <= max_chars:
        return redacted
    omitted = len(redacted) - max_chars
    return f"{redacted[:max_chars]}…<truncated {omitted} chars>"


def _safe_repr(value: Any) -> str:
    try:
        return repr(value)
    except Exception as exc:
        return f"<{type(value).__name__} repr failed: {type(exc).__name__}>"


def _omitted_items(total: int | None, retained: int) -> dict[str, Any]:
    omitted = None if total is None else max(0, total - retained)
    return {
        _AUDIT_TRUNCATED_KEY: {
            "reason": "item_limit",
            "retained": retained,
            "omitted": omitted,
        }
    }


def _sanitize_audit_value(
    value: Any,
    *,
    field_name: str | None = None,
    depth: int = 0,
    seen: set[int] | None = None,
    max_depth: int = _AUDIT_MAX_DEPTH,
    max_items: int = _AUDIT_MAX_ITEMS,
    max_string_chars: int = _AUDIT_MAX_STRING_CHARS,
) -> Any:
    """Convert arbitrary values into redacted, portable, resource-bounded JSON data."""
    normalized_name = field_name or ""
    if normalized_name and _audit_key_is_sensitive(normalized_name):
        return "<redacted>"
    if depth > max_depth:
        return {
            _AUDIT_TRUNCATED_KEY: {
                "reason": "depth_limit",
                "max_depth": max_depth,
                "type": type(value).__name__,
            }
        }
    if value is None or isinstance(value, bool | int):
        return value
    if isinstance(value, float):
        return (
            value
            if math.isfinite(value)
            else _bounded_text(repr(value), max_string_chars)
        )
    if isinstance(value, str):
        return _bounded_text(value, max_string_chars)
    if isinstance(value, bytes | bytearray | memoryview):
        data = bytes(value)
        return {
            _AUDIT_BINARY_KEY: {
                "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        }
    if isinstance(value, Path):
        return _bounded_text(str(value), max_string_chars)
    if isinstance(value, Enum):
        return _sanitize_audit_value(
            value.value,
            field_name=field_name,
            depth=depth,
            seen=seen,
            max_depth=max_depth,
            max_items=max_items,
            max_string_chars=max_string_chars,
        )
    if isinstance(value, BaseModel):
        try:
            value = value.model_dump(mode="python", by_alias=True)
        except Exception:
            return _bounded_text(_safe_repr(value), max_string_chars)
    elif is_dataclass(value) and not isinstance(value, type):
        try:
            value = {
                item.name: getattr(value, item.name) for item in fields(value)
            }
        except Exception:
            return _bounded_text(_safe_repr(value), max_string_chars)

    if seen is None:
        seen = set()
    value_id = id(value)
    if isinstance(value, Mapping | list | tuple | set | frozenset):
        if value_id in seen:
            return {_AUDIT_CYCLE_KEY: type(value).__name__}
        seen.add(value_id)

    try:
        if isinstance(value, Mapping):
            result: dict[str, Any] = {}
            retained = 0
            try:
                total: int | None = len(value)
            except Exception:
                total = None
            for retained, (key, child) in enumerate(value.items()):
                if retained >= max_items:
                    result.update(_omitted_items(total, retained))
                    break
                key_text = _bounded_text(str(key), 256)
                if key_text in result:
                    key_text = f"{key_text}#{retained + 1}"
                result[key_text] = _sanitize_audit_value(
                    child,
                    field_name=str(key),
                    depth=depth + 1,
                    seen=seen,
                    max_depth=max_depth,
                    max_items=max_items,
                    max_string_chars=max_string_chars,
                )
            return result

        if isinstance(value, list | tuple):
            result_list: list[Any] = []
            redact_next = False
            total = len(value)
            for child in value[:max_items]:
                if redact_next:
                    result_list.append("<redacted>")
                    redact_next = False
                    continue
                result_list.append(
                    _sanitize_audit_value(
                        child,
                        depth=depth + 1,
                        seen=seen,
                        max_depth=max_depth,
                        max_items=max_items,
                        max_string_chars=max_string_chars,
                    )
                )
                if isinstance(child, str) and SENSITIVE_FLAG_RE.fullmatch(
                    child
                ):
                    redact_next = True
            if total > max_items:
                result_list.append(_omitted_items(total, max_items))
            return result_list

        if isinstance(value, set | frozenset):
            ordered = sorted(value, key=_safe_repr)
            return _sanitize_audit_value(
                ordered,
                depth=depth,
                seen=seen,
                max_depth=max_depth,
                max_items=max_items,
                max_string_chars=max_string_chars,
            )

        return _bounded_text(_safe_repr(value), max_string_chars)
    finally:
        if isinstance(value, Mapping | list | tuple | set | frozenset):
            seen.discard(value_id)


def _preview_audit_value(value: Any) -> Any:
    """Return a compact already-redacted preview for an oversized event."""
    return _sanitize_audit_value(
        value,
        max_depth=_AUDIT_PREVIEW_DEPTH,
        max_items=_AUDIT_PREVIEW_ITEMS,
        max_string_chars=_AUDIT_PREVIEW_STRING_CHARS,
    )


def _encode_record(record: dict[str, Any]) -> bytes:
    return (
        json.dumps(
            record,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _fit_record(record: dict[str, Any], max_bytes: int) -> bytes:
    """Fit one event inside its byte budget without dropping identity fields."""
    encoded = _encode_record(record)
    if len(encoded) <= max_bytes:
        return encoded

    original_bytes = len(encoded)
    identity_names = (
        "id",
        "ts",
        "event",
        "call_id",
        "transport",
        "tool",
        "session",
        "ok",
        "duration_ms",
    )
    compact = {name: record[name] for name in identity_names if name in record}
    compact[_AUDIT_TRUNCATED_KEY] = {
        "reason": "event_byte_limit",
        "original_bytes": original_bytes,
        "max_bytes": max_bytes,
        "omitted_fields": [],
    }

    omitted: list[str] = []
    for name, value in record.items():
        if name in compact:
            continue
        candidate = {**compact, name: _preview_audit_value(value)}
        candidate[_AUDIT_TRUNCATED_KEY]["omitted_fields"] = omitted
        if len(_encode_record(candidate)) <= max_bytes:
            compact = candidate
        else:
            omitted.append(name)
    compact[_AUDIT_TRUNCATED_KEY]["omitted_fields"] = omitted
    encoded = _encode_record(compact)
    if len(encoded) <= max_bytes:
        return encoded

    minimal = {
        name: compact[name]
        for name in ("id", "ts", "event", "call_id", "tool", "session", "ok")
        if name in compact
    }
    minimal[_AUDIT_TRUNCATED_KEY] = {
        "reason": "event_byte_limit",
        "original_bytes": original_bytes,
        "max_bytes": max_bytes,
        "omitted_fields": sorted(set(record) - set(minimal)),
    }
    encoded = _encode_record(minimal)
    if len(encoded) <= max_bytes:
        return encoded

    fallback = {
        "id": str(record.get("id") or "")[:32],
        "ts": record.get("ts"),
        "event": _bounded_text(str(record.get("event") or "audit_event"), 64),
        _AUDIT_TRUNCATED_KEY: "event exceeded configured byte limit",
    }
    encoded = _encode_record(fallback)
    return encoded if len(encoded) <= max_bytes else b""


@contextmanager
def _audit_transaction(path: Path) -> Generator[None]:
    """Serialize log append and retention across threads and processes."""
    with (
        _AUDIT_THREAD_LOCK,
        private_file_lock(path.with_name(path.name + ".lock")),
    ):
        yield


def current_audit_call_id() -> str | None:
    """Return the current public tool lifecycle id when called inside a tool."""
    return _CURRENT_AUDIT_CALL_ID.get()


@contextmanager
def audit_call_context(
    call_id: str, session_ids: tuple[str, ...] = ()
) -> Generator[None]:
    """Bind lifecycle and session ids while a public tool implementation runs."""
    call_token = _CURRENT_AUDIT_CALL_ID.set(call_id)
    session_token = _CURRENT_AUDIT_SESSION_IDS.set(session_ids)
    try:
        yield
    finally:
        _CURRENT_AUDIT_SESSION_IDS.reset(session_token)
        _CURRENT_AUDIT_CALL_ID.reset(call_token)


def _retention_units(lines: list[bytes]) -> list[list[tuple[int, bytes]]]:
    """Group tool-call start/end records so retention never splits completed pairs."""
    units: list[list[tuple[int, bytes]]] = []
    call_units: dict[str, list[tuple[int, bytes]]] = {}
    for index, raw_line in enumerate(lines):
        call_id = ""
        with contextlib.suppress(json.JSONDecodeError, UnicodeDecodeError):
            record = json.loads(raw_line)
            if isinstance(record, dict) and record.get("event") in {
                "tool_call_start",
                "tool_call_end",
            }:
                call_id = str(record.get("call_id") or "")
        if call_id:
            unit = call_units.get(call_id)
            if unit is None:
                unit = []
                call_units[call_id] = unit
                units.append(unit)
            unit.append((index, raw_line))
        else:
            units.append([(index, raw_line)])
    units.sort(key=lambda unit: max(item[0] for item in unit))
    return units


def _enforce_audit_log_limit(path: Path, max_bytes: int) -> bool:
    """Atomically retain recent complete records within the configured log budget."""
    if max_bytes <= 0 or not path.exists() or path.stat().st_size <= max_bytes:
        return False
    lines = path.read_bytes().splitlines(keepends=True)
    target_bytes = max(1, max_bytes // 2)
    selected: list[tuple[int, bytes]] = []
    selected_bytes = 0
    for unit in reversed(_retention_units(lines)):
        unit_bytes = sum(len(raw_line) for _, raw_line in unit)
        if unit_bytes > max_bytes:
            continue
        if selected and selected_bytes + unit_bytes > target_bytes:
            break
        selected.extend(unit)
        selected_bytes += unit_bytes
        if selected_bytes >= target_bytes:
            break
    selected.sort(key=lambda item: item[0])
    atomic_write_private_bytes(path, b"".join(line for _, line in selected))
    return True


def _record_payload_activity(
    records: list[dict[str, Any]], settings: Settings
) -> tuple[set[str], set[str]]:
    referenced: set[str] = set()
    active: set[str] = set()
    now = time.time()
    for record in records:
        for metadata in payload_reference_metadata(record):
            digest = str(metadata["sha256"])
            referenced.add(digest)
            created_at = metadata.get("created_at")
            with contextlib.suppress(TypeError, ValueError):
                if created_at is None:
                    raise ValueError("missing created_at")
                age = max(0.0, now - float(created_at))
                if settings.audit_payload_retention_s > 0 and (
                    age <= settings.audit_payload_retention_s
                ):
                    active.add(digest)
    return referenced, active


def _enforce_audit_retention(
    path: Path, settings: Settings, *, payload_changed: bool
) -> None:
    """Retain paired JSONL units and referenced payloads without rescanning every event."""
    log_changed = _enforce_audit_log_limit(path, settings.max_audit_log_bytes)
    payload_root_exists = settings.audit_payload_dir.exists()
    sweep_key = str(path)
    monotonic_now = time.monotonic()
    last_sweep = _AUDIT_PAYLOAD_SWEEP_TIMES.get(sweep_key)
    periodic_sweep_due = payload_root_exists and (
        last_sweep is None
        or monotonic_now - last_sweep >= _AUDIT_PAYLOAD_SWEEP_INTERVAL_S
    )
    if not payload_changed and not log_changed and not periodic_sweep_due:
        return
    _AUDIT_PAYLOAD_SWEEP_TIMES[sweep_key] = monotonic_now
    if not path.exists():
        prune_payload_files(settings, referenced=set(), active_references=set())
        return

    lines = path.read_bytes().splitlines(keepends=True)
    units = _retention_units(lines)
    sizes = payload_file_sizes(settings)

    def unit_digests(unit: list[tuple[int, bytes]]) -> set[str]:
        found: set[str] = set()
        for _, raw_line in unit:
            with contextlib.suppress(json.JSONDecodeError, UnicodeDecodeError):
                value = json.loads(raw_line)
                found.update(payload_reference_digests(value))
        return found

    selected = list(units)
    referenced = (
        set().union(*(unit_digests(unit) for unit in selected))
        if selected
        else set()
    )
    while (
        selected
        and sum(sizes.get(digest, 0) for digest in referenced)
        > settings.max_audit_payload_store_bytes
    ):
        selected.pop(0)
        referenced = (
            set().union(*(unit_digests(unit) for unit in selected))
            if selected
            else set()
        )
    if len(selected) != len(units):
        kept = sorted(
            (item for unit in selected for item in unit),
            key=lambda item: item[0],
        )
        atomic_write_private_bytes(path, b"".join(raw for _, raw in kept))
        lines = [raw for _, raw in kept]

    records: list[dict[str, Any]] = []
    for raw_line in lines:
        with contextlib.suppress(json.JSONDecodeError, UnicodeDecodeError):
            value = json.loads(raw_line)
            if isinstance(value, dict):
                records.append(value)
    referenced, active = _record_payload_activity(records, settings)
    prune_payload_files(
        settings, referenced=referenced, active_references=active
    )


_AUDIT_SOURCE_INDEXES = "_source_indexes"
_AUDIT_QUERY_MAX_BYTES = 4_000_000
_AUDIT_QUERY_MAX_ENTRIES = 2_000
_AUDIT_HIDDEN_EVENTS = frozenset({"auth_ok"})
_AUDIT_LIFECYCLE_EVENTS = frozenset({"tool_call_start", "tool_call_end"})

_AUDIT_FILE_TOOLS = frozenset(
    {
        "delete_file_or_dir",
        "edit_lines",
        "glob_search",
        "hashline_edit",
        "list_files",
        "read",
        "search",
        "secret_scan",
        "tree_view",
        "write_file",
    }
)
_AUDIT_SHELL_TOOLS = frozenset(
    {
        "bash",
        "kill_persistent_shell",
        "list_persistent_shells",
        "read_persistent_shell_output",
        "resize_persistent_shell",
        "run_python_code",
        "send_persistent_shell_input",
        "session_start",
    }
)


def _audit_operation(record: dict[str, Any]) -> str:
    """Return one stable UI operation category for an audit record."""
    tool = str(record.get("tool") or "")
    event = str(record.get("event") or "")
    if tool in _AUDIT_FILE_TOOLS:
        return "files"
    if tool in _AUDIT_SHELL_TOOLS:
        return "shell"
    if tool == "job" or event.startswith("job_"):
        return "jobs"
    if tool.startswith("transfer_") or event.startswith(
        ("download_", "file_link_", "transfer_")
    ):
        return "transfer"
    if tool.startswith("remote_") or event.startswith("remote_"):
        return "remote"
    if tool.startswith("agent_") or event.startswith(("agent_", "skill_")):
        return "agent"
    if event.startswith(("run_shell_", "shell_", "ui_terminal_")):
        return "shell"
    if event.startswith("oauth_") or event.startswith("mcp_"):
        return "security"
    return "other"


def _audit_node(record: dict[str, Any]) -> str:
    return str(record.get("machine") or record.get("node") or "local")


def _audit_session(record: dict[str, Any]) -> str:
    direct = (
        record.get("session_id")
        or record.get("session")
        or record.get("shell_id")
    )
    if direct:
        return str(direct)
    call_input = _audit_call_input(record)
    if isinstance(call_input, dict):
        nested = (
            call_input.get("session_id")
            or call_input.get("session")
            or call_input.get("shell_id")
        )
        if nested:
            return str(nested)
    return ""


def _audit_call_key(record: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(record.get("tool") or ""),
        _audit_node(record),
        _audit_session(record),
    )


def _audit_call_input(record: dict[str, Any]) -> Any:
    if "input" in record:
        return record["input"]
    arguments = record.get("arguments")
    if isinstance(arguments, dict):
        return arguments.get("keyword_args", arguments)
    return None


def _new_audit_call_entry(record: dict[str, Any], index: int) -> dict[str, Any]:
    call_id = str(record.get("call_id") or "")
    entry: dict[str, Any] = {
        "id": (
            f"call:{call_id}"
            if call_id
            else f"legacy-call:{record.get('ts', 0)}:{index}"
        ),
        "ts": _audit_timestamp(record.get("ts")),
        "event": "tool_call",
        "tool": str(record.get("tool") or "unknown"),
        "node": _audit_node(record),
        "operation": _audit_operation(record),
        "paired": False,
        "status": "running",
        "source_events": ["tool_call_start"],
        _AUDIT_SOURCE_INDEXES: [index],
    }
    if call_id:
        entry["call_id"] = call_id
    session = _audit_session(record)
    if session:
        entry["session"] = session
    call_input = _audit_call_input(record)
    if call_input is not None:
        entry["input"] = call_input
    return entry


def _finish_audit_call_entry(
    entry: dict[str, Any], record: dict[str, Any], index: int
) -> None:
    ok = record.get("ok") if isinstance(record.get("ok"), bool) else None
    entry["paired"] = True
    entry["status"] = (
        "success" if ok is True else "failed" if ok is False else "completed"
    )
    entry["source_events"] = ["tool_call_start", "tool_call_end"]
    entry[_AUDIT_SOURCE_INDEXES].append(index)
    if ok is not None:
        entry["ok"] = ok
    for name in ("duration_ms", "output", "error", "error_type"):
        if name in record:
            entry[name] = record[name]


def _unpaired_audit_end_entry(
    record: dict[str, Any], index: int
) -> dict[str, Any]:
    call_id = str(record.get("call_id") or "")
    entry: dict[str, Any] = {
        "id": (
            f"call:{call_id}"
            if call_id
            else f"legacy-end:{record.get('ts', 0)}:{index}"
        ),
        "ts": _audit_timestamp(record.get("ts")),
        "event": "tool_call",
        "tool": str(record.get("tool") or "unknown"),
        "node": _audit_node(record),
        "operation": _audit_operation(record),
        "paired": False,
        "status": "unpaired",
        "source_events": ["tool_call_end"],
        _AUDIT_SOURCE_INDEXES: [index],
    }
    if call_id:
        entry["call_id"] = call_id
    session = _audit_session(record)
    if session:
        entry["session"] = session
    for name in ("ok", "duration_ms", "output", "error", "error_type"):
        if name in record:
            entry[name] = record[name]
    return entry


def _nested_audit_event(record: dict[str, Any]) -> dict[str, Any] | None:
    event = str(record.get("event") or "")
    if not event or event in _AUDIT_LIFECYCLE_EVENTS:
        return None
    return {
        name: value
        for name, value in record.items()
        if name not in {"id", "ts", "parent_call_id"}
    }


def _coalesce_audit_records(
    records: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Pair tool calls and fold child events into stable Human UI rows."""
    rows: list[dict[str, Any]] = []
    pending_by_id: dict[str, dict[str, Any]] = {}
    entries_by_id: dict[str, dict[str, Any]] = {}
    pending_legacy: dict[tuple[str, str, str], list[dict[str, Any]]] = {}

    for index, record in enumerate(records):
        event = str(record.get("event") or "")
        if event in _AUDIT_HIDDEN_EVENTS:
            continue
        parent_call_id = str(record.get("parent_call_id") or "")
        if parent_call_id:
            parent = entries_by_id.get(parent_call_id)
            if parent is not None:
                parent[_AUDIT_SOURCE_INDEXES].append(index)
                nested = _nested_audit_event(record)
                if nested is not None:
                    parent.setdefault("related_events", []).append(nested)
                continue
            if event in _AUDIT_LIFECYCLE_EVENTS:
                continue
        if event == "tool_call_start":
            entry = _new_audit_call_entry(record, index)
            rows.append(entry)
            call_id = str(record.get("call_id") or "")
            if call_id:
                pending_by_id[call_id] = entry
                entries_by_id[call_id] = entry
            else:
                pending_legacy.setdefault(_audit_call_key(record), []).append(
                    entry
                )
            continue
        if event == "tool_call_end":
            call_id = str(record.get("call_id") or "")
            entry = pending_by_id.pop(call_id, None) if call_id else None
            if entry is None and not call_id:
                pending = pending_legacy.get(_audit_call_key(record), [])
                if pending:
                    entry = pending.pop(0)
            if entry is None:
                rows.append(_unpaired_audit_end_entry(record, index))
            else:
                _finish_audit_call_entry(entry, record, index)
            continue

        row = {
            **record,
            "ts": _audit_timestamp(record.get("ts")),
            "id": str(
                record.get("id") or f"record:{record.get('ts', 0)}:{index}"
            ),
            "node": _audit_node(record),
            "operation": _audit_operation(record),
            _AUDIT_SOURCE_INDEXES: [index],
        }
        session = _audit_session(record)
        if session and not row.get("session"):
            row["session"] = session
        rows.append(row)
    return rows


def _public_audit_entry(row: dict[str, Any]) -> dict[str, Any]:
    return {
        name: value
        for name, value in row.items()
        if name != _AUDIT_SOURCE_INDEXES
    }


def summarize_audit_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """Return metadata-only audit data suitable for bounded list transport."""
    summary: dict[str, Any] = {
        "id": entry.get("id"),
        "ts": entry.get("ts"),
        "event": entry.get("event"),
        "node": entry.get("node"),
        "operation": entry.get("operation"),
    }
    for name in (
        "tool",
        "status",
        "paired",
        "ok",
        "duration_ms",
        "session",
        "call_id",
    ):
        if name in entry:
            summary[name] = entry[name]
    source_events = entry.get("source_events")
    if isinstance(source_events, list):
        summary["source_events"] = [str(value) for value in source_events[:8]]
    related_events = entry.get("related_events")
    if isinstance(related_events, list):
        summary["related_event_count"] = len(related_events)
    return summary


def audit_query_snapshot(
    result: dict[str, Any], *, selected_id: str | None = None
) -> dict[str, Any]:
    """Return list summaries plus one preview entry from a single audit scan."""
    rows = result.get("entries")
    if not isinstance(rows, list):
        raise ValueError("audit query result entries must be a list")
    entries = [entry for entry in rows if isinstance(entry, dict)]
    normalized_selected_id = str(selected_id or "").strip()
    selected = next(
        (
            entry
            for entry in entries
            if normalized_selected_id
            and str(entry.get("id") or "") == normalized_selected_id
        ),
        entries[0] if entries else None,
    )
    snapshot = {
        **result,
        "entries": [summarize_audit_entry(entry) for entry in entries],
    }
    if selected is not None:
        snapshot["entry"] = selected
    return snapshot


def _read_audit_records(path: Path | None = None) -> list[dict[str, Any]]:
    """Read a bounded, consistent tail of the private JSONL audit log."""
    settings = get_settings()
    path = path or settings.audit_log_path
    configured_limit = int(settings.max_audit_log_bytes)
    max_bytes = _AUDIT_QUERY_MAX_BYTES
    if configured_limit > 0:
        max_bytes = min(max_bytes, configured_limit)
    max_bytes = max(1, max_bytes)

    with _audit_transaction(path):
        if not path.exists():
            return []
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size > max_bytes:
                handle.seek(size - max_bytes)
                handle.readline()
            raw = handle.read(max_bytes)

    records: list[dict[str, Any]] = []
    for line in raw.splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError, UnicodeDecodeError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def _audit_timestamp(value: Any) -> float:
    """Return a finite sortable timestamp, treating malformed values as zero."""
    try:
        timestamp = float(value or 0)
    except TypeError, ValueError:
        return 0.0
    return timestamp if math.isfinite(timestamp) else 0.0


def _audit_search_document(row: dict[str, Any]) -> str:
    """Return searchable summary metadata without indexing protected payloads."""
    related_events = row.get("related_events")
    related_names = (
        [
            str(event.get("event") or "")
            for event in related_events
            if isinstance(event, dict)
        ]
        if isinstance(related_events, list)
        else []
    )
    summary = {
        "id": row.get("id"),
        "event": row.get("event"),
        "source_events": row.get("source_events"),
        "related_events": related_names,
        "tool": row.get("tool"),
        "node": row.get("node"),
        "operation": row.get("operation"),
        "session": row.get("session"),
        "status": row.get("status"),
        "ok": row.get("ok"),
        "call_id": row.get("call_id"),
    }
    return json.dumps(summary, ensure_ascii=False, default=str).casefold()


def query_audit(
    *,
    limit: int = 300,
    event: str | None = None,
    operation: str | None = None,
    session: str | None = None,
    search: str | None = None,
    start_ts: float | None = None,
    end_ts: float | None = None,
    sort: str = "desc",
    exclude_call_id: str | None = None,
    _path: Path | None = None,
) -> dict[str, Any]:
    """Read, pair, filter, and sort bounded audit rows for a Human UI client."""
    bounded_limit = max(1, min(int(limit), _AUDIT_QUERY_MAX_ENTRIES))
    normalized_sort = str(sort).casefold()
    if normalized_sort not in {"asc", "desc"}:
        raise ValueError("sort must be asc or desc")
    if start_ts is not None and end_ts is not None and start_ts > end_ts:
        raise ValueError("start_ts must not be greater than end_ts")

    rows = _coalesce_audit_records(_read_audit_records(_path))
    needle = (search or "").casefold().strip()
    event_filter = (event or "").casefold().strip()
    operation_filter = (operation or "").casefold().strip()
    session_filter = (session or "").casefold().strip()
    matched: list[dict[str, Any]] = []
    for row in rows:
        if exclude_call_id and str(row.get("call_id") or "") == exclude_call_id:
            continue
        timestamp = _audit_timestamp(row.get("ts"))
        if start_ts is not None and timestamp < start_ts:
            continue
        if end_ts is not None and timestamp > end_ts:
            continue
        event_text = " ".join(
            [
                str(row.get("event") or ""),
                *map(str, row.get("source_events") or []),
            ]
        )
        if event_filter and event_filter not in event_text.casefold():
            continue
        if (
            operation_filter
            and operation_filter != str(row.get("operation") or "").casefold()
        ):
            continue
        if (
            session_filter
            and session_filter != str(row.get("session") or "").casefold()
        ):
            continue
        if needle and needle not in _audit_search_document(row):
            continue
        matched.append(row)

    matched.sort(
        key=lambda item: _audit_timestamp(item.get("ts")),
        reverse=normalized_sort == "desc",
    )
    total = len(matched)
    failed_matched = sum(
        row.get("ok") is False or str(row.get("status") or "") == "failed"
        for row in matched
    )
    selected = matched[:bounded_limit]
    entries = [_public_audit_entry(row) for row in selected]
    return {
        "entries": entries,
        "count": len(selected),
        "total_matched": total,
        "failed_matched": failed_matched,
    }


def get_audit_entry(
    entry_id: str,
    *,
    include_full_payloads: bool = False,
    exclude_call_id: str | None = None,
    _path: Path | None = None,
) -> dict[str, Any]:
    """Return one coalesced audit entry, optionally resolving sanitized payloads."""
    normalized = str(entry_id).strip()
    if not normalized:
        raise ValueError("audit entry id is required")
    for row in _coalesce_audit_records(_read_audit_records(_path)):
        if exclude_call_id and str(row.get("call_id") or "") == exclude_call_id:
            continue
        if str(row.get("id") or "") == normalized:
            entry = _public_audit_entry(row)
            if include_full_payloads:
                return resolve_payload_references(entry, get_settings())
            return entry
    raise ValueError(f"Unknown audit entry: {normalized}")


def query_session_audit(
    session_id: str,
    **filters: Any,
) -> dict[str, Any]:
    """Query the dedicated append-only audit log for one durable session."""
    return query_audit(
        **filters,
        _path=get_state_store().layout.session_audit_path(session_id),
    )


def get_session_audit_entry(
    session_id: str,
    entry_id: str,
    *,
    include_full_payloads: bool = False,
) -> dict[str, Any]:
    """Return one entry from a session's dedicated audit log."""
    return get_audit_entry(
        entry_id,
        include_full_payloads=include_full_payloads,
        _path=get_state_store().layout.session_audit_path(session_id),
    )


def _audit_record_session_ids(fields: Mapping[str, Any]) -> tuple[str, ...]:
    """Return explicit or context-bound sessions that own one audit record."""
    explicit: list[str] = []
    session_ids = fields.get("session_ids")
    if isinstance(session_ids, list | tuple):
        explicit.extend(
            value for value in session_ids if isinstance(value, str) and value
        )
    for name in ("session_id", "session"):
        value = fields.get(name)
        if isinstance(value, str) and value:
            explicit.append(value)
    if not explicit:
        explicit.extend(tool_input_session_ids(dict(fields)))
    if not explicit:
        explicit.extend(_CURRENT_AUDIT_SESSION_IDS.get())
    return tuple(dict.fromkeys(explicit))


def _append_session_audit_records(
    session_ids: tuple[str, ...], encoded: bytes, settings: Settings
) -> None:
    """Append one sanitized record to each existing owning session log."""
    state_store = get_state_store()
    for session_id in session_ids:
        try:
            transaction_path = state_store.layout.session_transaction_path(
                session_id
            )
            metadata_path = state_store.layout.session_metadata_path(session_id)
            path = state_store.layout.session_audit_path(session_id)
        except ValueError:
            continue
        with state_store.transaction(transaction_path):
            if not metadata_path.is_file():
                continue
            with _audit_transaction(path):
                append_private_bytes(path, encoded)
                _enforce_audit_log_limit(path, settings.max_audit_log_bytes)


def audit(event: str, **fields: Any) -> None:
    """Append one uniformly redacted, bounded, private audit record."""
    settings = get_settings()
    parent_call_id = current_audit_call_id()
    if parent_call_id and "parent_call_id" not in fields:
        fields = {**fields, "parent_call_id": parent_call_id}
    session_ids = _audit_record_session_ids(fields)
    event_limit = max(512, int(settings.max_audit_event_bytes))
    if settings.max_audit_log_bytes > 0:
        event_limit = min(
            event_limit, max(512, int(settings.max_audit_log_bytes))
        )
    created_at = time.time()
    recoverable_string_chars = (
        settings.max_audit_payload_bytes
        if settings.audit_payloads_enabled
        else event_limit
    )
    full_string_chars = max(
        _AUDIT_MAX_STRING_CHARS, int(recoverable_string_chars)
    )
    sanitized = {
        str(name): _sanitize_audit_value(
            value,
            field_name=str(name),
            max_string_chars=full_string_chars,
        )
        for name, value in fields.items()
    }
    path: Path = settings.audit_log_path
    with _audit_transaction(path):
        externalized = {
            name: externalize_sanitized_value(
                value,
                settings=settings,
                preview=_preview_audit_value(value),
                created_at=created_at,
            )
            for name, value in sanitized.items()
        }
        record = {
            "id": uuid.uuid4().hex,
            "ts": created_at,
            "event": _bounded_text(str(event), 256),
            **externalized,
        }
        encoded = _fit_record(record, event_limit)
        if not encoded:
            raise RuntimeError(
                "Audit event byte limit is too small to retain event identity"
            )
        append_private_bytes(path, encoded)
        _enforce_audit_retention(
            path,
            settings,
            payload_changed=bool(payload_reference_digests(externalized)),
        )
    if session_ids:
        _append_session_audit_records(session_ids, encoded, settings)


def new_audit_call_id() -> str:
    """Return a unique identifier used to link start/end audit records for one operation."""
    return uuid.uuid4().hex


def audit_tool_call_start(
    *,
    call_id: str,
    transport: str,
    tool: str,
    input: Any,
) -> tuple[str, ...]:
    """Record the bounded input and caller context for one tool call."""
    fields: dict[str, Any] = {
        "call_id": call_id,
        "transport": transport,
        "tool": tool,
        "input": input,
    }
    session_ids = tool_input_session_ids(input)
    if session_ids:
        fields["session"] = session_ids[0]
        fields["session_ids"] = list(session_ids)
    audit("tool_call_start", **fields)
    return session_ids


def audit_tool_call_end(
    *,
    call_id: str,
    transport: str,
    tool: str,
    ok: bool,
    duration_ms: int,
    output: Any = None,
    error: dict[str, Any] | None = None,
    session_ids: tuple[str, ...] = (),
) -> None:
    """Record the bounded output or nested exception details for one tool call."""
    fields: dict[str, Any] = {
        "call_id": call_id,
        "transport": transport,
        "tool": tool,
        "ok": ok,
        "duration_ms": duration_ms,
    }
    if session_ids:
        fields["session"] = session_ids[0]
        fields["session_ids"] = list(session_ids)
    if error is not None:
        fields["error"] = error
    else:
        fields["output"] = output
    audit("tool_call_end", **fields)
