"""Field-scoped memory content protection from LibreChat's memory pipeline.

The controller has no Node runtime. Custom regular expressions use Python's
regex engine with a strict timeout and fail-closed projection on errors.
"""

import time
from typing import Any

from .content_filters import ContentFragment, inspect_content
from .content_patterns import _rules

_MEMORY_FIELDS = ("key", "value", "summary")
_MAX_FIELD_LENGTH = 262144
_MAX_SCAN_SECONDS = 1.0


class MemoryContentBlocked(ValueError):
    def __init__(self, field: str, label: str = "protected value") -> None:
        self.field, self.label = field, label
        super().__init__("Memory content blocked by configured policy")

    def response(self, *, model: bool = False) -> dict:
        return {
            "error": "content_filter_block",
            "message": (
                "Submitted content was blocked by content policy."
                if model
                else "Submitted content contains protected information. Remove it and try again."
            ),
            "source": "memory",
            "field": self.field,
        }


def configured_filters(repo: Any, owner: str) -> dict | None:
    configured = repo.setting(owner, "webui.filters.config")
    if not isinstance(configured, dict):
        endpoints = repo.setting(owner, "endpoints", {}) or {}
        configured = (
            endpoints.get("filters") if isinstance(endpoints, dict) else None
        )
    return configured if isinstance(configured, dict) else None


def _policy(filters: dict | None) -> dict | None:
    if not isinstance(filters, dict):
        return None
    memory = filters.get("memories")
    policy = memory.get("pii") if isinstance(memory, dict) else None
    if not isinstance(policy, dict):
        return None
    if policy.get("starterPatterns") == [] and not policy.get("customPatterns"):
        return None
    return policy


def _fields(policy: dict) -> tuple[str, ...]:
    values = policy.get("fields")
    if values is None:
        return _MEMORY_FIELDS
    if not isinstance(values, list) or len(values) > 256:
        return _MEMORY_FIELDS
    return tuple(field for field in _MEMORY_FIELDS if field in values)


def inspect_memory_content(
    entry: dict, filters: dict | None
) -> MemoryContentBlocked | None:
    policy = _policy(filters)
    if policy is None:
        return None
    if policy.get("action") == "audit":
        inspect_content(
            [
                ContentFragment("memory", field, value, "user")
                for field, value in entry.items()
                if isinstance(value, str)
            ],
            filters,
        )
        return None
    fields = _fields(policy)
    if not fields:
        return None
    try:
        rules = _rules(policy)
    except Exception:
        # Invalid configuration or missing optional regex must fail closed,
        # never block Controller startup or expose already-stored values.
        return MemoryContentBlocked(fields[0])
    deadline = time.monotonic() + _MAX_SCAN_SECONDS
    for field in fields:
        content = entry.get(field)
        if not isinstance(content, str):
            continue
        if len(content) > _MAX_FIELD_LENGTH:
            return MemoryContentBlocked(field)
        for compiled, label in rules:
            try:
                remaining = deadline - time.monotonic()
                if (
                    remaining <= 0
                    or compiled.search(content, timeout=min(0.025, remaining))
                    is not None
                ):
                    return MemoryContentBlocked(field, label)
            except TimeoutError:
                return MemoryContentBlocked(field)
    return None


def project_stored_memories(
    records: list[dict], filters: dict | None
) -> list[dict]:
    """Project old rows under current policy while retaining management metadata."""
    policy = _policy(filters)
    if policy is None:
        return records
    if policy.get("action") == "audit":
        for record in records:
            inspect_memory_content(record, filters)
        return records
    selected = _fields(policy)
    projected = []
    for record in records:
        copy = dict(record)
        for field in selected:
            if field not in record:
                continue
            field_policy = {"memories": {"pii": {**policy, "fields": [field]}}}
            if inspect_memory_content({field: record[field]}, field_policy):
                copy[field] = ""
                copy["contentFilterBlocked"] = True
        projected.append(copy)
    return projected


def assert_memory_content_allowed(
    key: str, value: str, filters: dict | None
) -> None:
    finding = inspect_memory_content({"key": key, "value": value}, filters)
    if finding is not None:
        raise finding
