"""Authenticated Human UI API for executor Dashboard telemetry."""

import math
from typing import Any

from fastapi import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from ...control.ui_executor import call_ui_executor
from ...oauth.core.context import MissingOAuthScopeError, require_oauth_scopes
from ...oauth.core.scopes import SCOPE_SHELL_READ
from ..dashboard import dashboard_snapshot
from .common import (
    bounded_text as _bounded_text,
)
from .common import (
    json_error as _json_error,
)

UI_DASHBOARD_EXECUTOR_MAX_BYTES = 255
UI_DASHBOARD_TEXT_MAX_BYTES = 4_096
UI_DASHBOARD_MAX_ALERTS = 12
UI_DASHBOARD_MAX_ACTIVITY = 12

_SYSTEM_NUMBER_FIELDS = (
    "timestamp",
    "cpu_percent",
    "cpu_count",
    "memory_percent",
    "memory_used_bytes",
    "memory_total_bytes",
    "disk_percent",
    "disk_used_bytes",
    "disk_total_bytes",
    "load_1m",
    "network_rx_bps",
    "network_tx_bps",
    "uptime_s",
)


def _json_ok(data: Any = None, message: str = "") -> JSONResponse:
    return JSONResponse({"ok": True, "message": message, "data": data})


def _require_scopes(*required: str) -> None:
    try:
        require_oauth_scopes(tuple(dict.fromkeys(required)))
    except MissingOAuthScopeError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


def _executor_id_arg(value: Any) -> str:
    return _bounded_text(
        value,
        field="executor_id",
        max_bytes=UI_DASHBOARD_EXECUTOR_MAX_BYTES,
        allow_empty=False,
    )


def _finite_number(
    value: Any,
    *,
    field: str,
    minimum: float | None = None,
    maximum: float | None = None,
) -> int | float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RuntimeError(f"Malformed dashboard {field}")
    normalized = float(value)
    if not math.isfinite(normalized):
        raise RuntimeError(f"Malformed dashboard {field}")
    if minimum is not None and normalized < minimum:
        raise RuntimeError(f"Malformed dashboard {field}")
    if maximum is not None and normalized > maximum:
        raise RuntimeError(f"Malformed dashboard {field}")
    if field == "cpu_count":
        if normalized < 1 or not normalized.is_integer():
            raise RuntimeError("Malformed dashboard cpu_count")
        return int(normalized)
    return normalized


def _snapshot_text(
    value: Any,
    *,
    field: str,
    max_bytes: int,
    allow_empty: bool = True,
) -> str:
    try:
        return _bounded_text(
            value,
            field=field,
            max_bytes=max_bytes,
            allow_empty=allow_empty,
        )
    except ValueError as exc:
        raise RuntimeError(f"Malformed dashboard {field}") from exc


def _normalize_system(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RuntimeError("Executor returned malformed dashboard system data")
    percentage_fields = {"cpu_percent", "memory_percent", "disk_percent"}
    system: dict[str, Any] = {}
    for field in _SYSTEM_NUMBER_FIELDS:
        system[field] = _finite_number(
            value.get(field),
            field=field,
            minimum=0.0 if field != "cpu_count" else None,
            maximum=100.0 if field in percentage_fields else None,
        )
    return system


def _normalize_version(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        raise RuntimeError("Executor returned malformed dashboard version data")
    version: dict[str, str] = {}
    for field in ("version", "package_version", "python", "platform"):
        if field not in value:
            continue
        version[field] = _snapshot_text(
            value.get(field),
            field=f"version.{field}",
            max_bytes=512,
        )
    return version


def _normalize_alert(executor_id: str, value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RuntimeError("Executor returned a malformed dashboard alert")
    severity = str(value.get("severity") or "info").casefold()
    if severity not in {"info", "warning", "critical"}:
        severity = "info"
    alert: dict[str, Any] = {
        "severity": severity,
        "title": _snapshot_text(
            value.get("title"),
            field="alert title",
            max_bytes=1_024,
            allow_empty=False,
        ),
        "detail": _snapshot_text(
            value.get("detail"),
            field="alert detail",
            max_bytes=UI_DASHBOARD_TEXT_MAX_BYTES,
        ),
        "node": executor_id,
    }
    if value.get("age_s") is not None:
        alert["age_s"] = _finite_number(
            value.get("age_s"), field="alert age", minimum=0.0
        )
    return alert


def _normalize_activity(executor_id: str, value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RuntimeError("Executor returned malformed dashboard activity")
    kind = str(value.get("kind") or "success").casefold()
    if kind not in {"success", "running", "failed"}:
        kind = "success"
    activity: dict[str, Any] = {
        "timestamp": _finite_number(
            value.get("timestamp"), field="activity timestamp", minimum=0.0
        )
        or 0.0,
        "kind": kind,
        "title": _snapshot_text(
            value.get("title"),
            field="activity title",
            max_bytes=1_024,
            allow_empty=False,
        ),
        "detail": _snapshot_text(
            value.get("detail"),
            field="activity detail",
            max_bytes=1_024,
        ),
        "node": executor_id,
    }
    if value.get("duration_ms") is not None:
        activity["duration_ms"] = _finite_number(
            value.get("duration_ms"), field="activity duration", minimum=0.0
        )
    return activity


def _bounded_nonnegative_int(value: Any, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RuntimeError(f"Executor returned malformed dashboard {field}")
    return value


def _normalize_snapshot(executor_id: str, value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RuntimeError(
            f"Executor {executor_id} returned malformed dashboard data"
        )
    health = str(value.get("health") or "healthy").casefold()
    if health not in {"healthy", "attention", "critical"}:
        raise RuntimeError(
            f"Executor {executor_id} returned malformed dashboard health"
        )
    raw_alerts = value.get("alerts")
    raw_activity = value.get("activity")
    if (
        not isinstance(raw_alerts, list)
        or len(raw_alerts) > UI_DASHBOARD_MAX_ALERTS
    ):
        raise RuntimeError(
            f"Executor {executor_id} returned malformed dashboard alerts"
        )
    if (
        not isinstance(raw_activity, list)
        or len(raw_activity) > UI_DASHBOARD_MAX_ACTIVITY
    ):
        raise RuntimeError(
            f"Executor {executor_id} returned malformed dashboard activity"
        )
    sources = value.get("sources")
    if not isinstance(sources, dict):
        raise RuntimeError(
            f"Executor {executor_id} returned malformed dashboard sources"
        )
    audit_total = _bounded_nonnegative_int(
        value.get("audit_total_24h"), field="audit_total_24h"
    )
    audit_failed = _bounded_nonnegative_int(
        value.get("audit_failed_24h"), field="audit_failed_24h"
    )
    if audit_failed > audit_total:
        raise RuntimeError(
            f"Executor {executor_id} returned malformed dashboard Audit counts"
        )
    system_source = _snapshot_text(
        sources.get("system"),
        field="system source",
        max_bytes=32,
        allow_empty=False,
    )
    audit_source = _snapshot_text(
        sources.get("audit"),
        field="audit source",
        max_bytes=32,
        allow_empty=False,
    )
    if system_source not in {"ok", "degraded"} or audit_source not in {
        "ok",
        "degraded",
    }:
        raise RuntimeError(
            f"Executor {executor_id} returned malformed dashboard source states"
        )
    return {
        "executor_id": executor_id,
        "generated_at": _finite_number(
            value.get("generated_at"), field="generated_at", minimum=0.0
        )
        or 0.0,
        "health": health,
        "version": _normalize_version(value.get("version")),
        "system": _normalize_system(value.get("system")),
        "alerts": [_normalize_alert(executor_id, item) for item in raw_alerts],
        "activity": [
            _normalize_activity(executor_id, item) for item in raw_activity
        ],
        "audit_total_24h": audit_total,
        "audit_failed_24h": audit_failed,
        "sources": {"system": system_source, "audit": audit_source},
    }


async def _snapshot(request: Request, executor_id: str) -> dict[str, Any]:
    runtime = getattr(request.app.state, "control_runtime", None)
    if runtime is None:
        raise RuntimeError("Human UI Dashboard requires the control runtime")
    resolved_executor_id, value = await call_ui_executor(
        runtime,
        executor_id,
        "ui.dashboard.snapshot",
    )
    if not isinstance(value, dict):
        raise RuntimeError(
            f"Executor {resolved_executor_id} returned malformed dashboard data"
        )
    return _normalize_snapshot(resolved_executor_id, dashboard_snapshot(value))


async def api_dashboard(request: Request) -> Response:
    """Return one executor-scoped Dashboard snapshot."""
    try:
        executor_id = _executor_id_arg(request.query_params.get("executor_id"))
        _require_scopes(SCOPE_SHELL_READ)
        return _json_ok(await _snapshot(request, executor_id))
    except HTTPException:
        raise
    except ValueError as exc:
        return _json_error(exc, status_code=400)
    except ConnectionError as exc:
        return _json_error(exc, status_code=503)
    except RuntimeError as exc:
        return _json_error(exc, status_code=502)
    except Exception as exc:
        return _json_error(exc)
