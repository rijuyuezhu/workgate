"""Dependency-free GUI input and result limits, shared with the AT-SPI subprocess."""

import json
from collections.abc import Callable, Iterable
from typing import Any

GUI_MAX_ELEMENTS = 1000
GUI_MAX_DEPTH = 20
GUI_MAX_WINDOWS = 256
GUI_MAX_WINDOW_ID_BYTES = 1024
GUI_MAX_WINDOW_TEXT_BYTES = 1024
GUI_MAX_WINDOWS_TOTAL_BYTES = 128 * 1024
GUI_MAX_ELEMENT_TEXT_BYTES = 1024
GUI_MAX_ELEMENT_VALUE_BYTES = 2048
GUI_MAX_ELEMENTS_TOTAL_BYTES = 64 * 1024
GUI_MAX_ELEMENT_ACTIONS = 32


def truncate_text(value: Any, limit: int) -> str:
    text = str(value or "")
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    suffix = "..."[: max(0, limit)]
    budget = max(0, limit - len(suffix))
    return encoded[:budget].decode("utf-8", errors="ignore") + suffix


def json_size(value: Any) -> int:
    return len(
        json.dumps(
            value, ensure_ascii=False, separators=(",", ":"), default=str
        ).encode("utf-8")
    )


def key_parts(keys: Any) -> list[str]:
    if isinstance(keys, str):
        parts = keys.replace("+", " ").split()
    elif isinstance(keys, list):
        parts = [str(part).strip() for part in keys if str(part).strip()]
    else:
        raise ValueError("key action requires keys as a string or list")
    if not parts:
        raise ValueError("key action requires at least one key")
    return parts


def _copy_bounds(source: dict[str, Any], target: dict[str, Any]) -> None:
    bounds = source.get("bounds")
    if isinstance(bounds, dict):
        target["bounds"] = {
            key: bounds[key]
            for key in ("x", "y", "width", "height")
            if key in bounds
        }


def bounded_element(element: dict[str, Any]) -> dict[str, Any]:
    bounded: dict[str, Any] = {
        key: truncate_text(element[key], GUI_MAX_ELEMENT_TEXT_BYTES)
        for key in ("id", "role", "name", "automation_id")
        if key in element
    }
    for key in ("value", "description"):
        if key in element:
            bounded[key] = truncate_text(
                element[key], GUI_MAX_ELEMENT_VALUE_BYTES
            )
    for key in ("enabled", "focused", "editable", "offscreen"):
        if key in element:
            bounded[key] = element[key]
    if "depth" in element:
        bounded["depth"] = element["depth"]
    _copy_bounds(element, bounded)
    actions = element.get("actions")
    if isinstance(actions, list):
        bounded["actions"] = [
            truncate_text(item, GUI_MAX_ELEMENT_TEXT_BYTES)
            for item in actions[:GUI_MAX_ELEMENT_ACTIONS]
        ]
    return bounded


def bounded_window(window: dict[str, Any]) -> dict[str, Any] | None:
    bounded: dict[str, Any] = {}
    if "id" in window:
        window_id = str(window.get("id") or "")
        if len(window_id.encode("utf-8")) > GUI_MAX_WINDOW_ID_BYTES:
            return None
        bounded["id"] = window_id
    for key in ("title", "app"):
        if key in window:
            bounded[key] = truncate_text(window[key], GUI_MAX_WINDOW_TEXT_BYTES)
    if "pid" in window:
        try:
            raw_pid = window.get("pid")
            bounded["pid"] = int(raw_pid) if raw_pid is not None else 0
        except (TypeError, ValueError, OverflowError):
            bounded["pid"] = 0
    _copy_bounds(window, bounded)
    return bounded


def bounded_records(
    records: Iterable[Any],
    project: Callable[[dict[str, Any]], dict[str, Any] | None],
    max_bytes: int,
) -> list[dict[str, Any]]:
    bounded = []
    used = 2
    for raw in records:
        if not isinstance(raw, dict):
            continue
        record = project(raw)
        if record is None:
            continue
        extra = json_size(record) + bool(bounded)
        if used + extra > max_bytes:
            break
        bounded.append(record)
        used += extra
    return bounded
