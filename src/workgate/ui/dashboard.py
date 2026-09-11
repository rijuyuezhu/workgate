"""Control-owned Human UI Dashboard projections over canonical Audit metadata."""

import math
import time
from typing import Any

from ..audit import query_audit

_DASHBOARD_AUDIT_LIMIT = 160
_DASHBOARD_WINDOW_S = 86_400
_DASHBOARD_MAX_ALERTS = 12
_DASHBOARD_MAX_ACTIVITY = 12


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    normalized = float(value)
    return normalized if math.isfinite(normalized) else None


def _audit_failed(entry: dict[str, Any]) -> bool:
    return (
        entry.get("ok") is False or str(entry.get("status") or "") == "failed"
    )


def dashboard_activity(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return metadata-only recent activity suitable for a read-only dashboard."""
    rows: list[dict[str, Any]] = []
    for entry in entries[:_DASHBOARD_MAX_ACTIVITY]:
        status = str(entry.get("status") or "")
        running = entry.get("paired") is False or status in {
            "running",
            "unpaired",
        }
        rows.append(
            {
                "timestamp": _finite_number(entry.get("ts")) or 0.0,
                "kind": "failed"
                if _audit_failed(entry)
                else "running"
                if running
                else "success",
                "title": str(
                    entry.get("tool") or entry.get("event") or "MCP activity"
                ),
                "detail": str(entry.get("operation") or "other"),
                "duration_ms": _finite_number(entry.get("duration_ms")),
            }
        )
    return rows


def dashboard_alerts(
    entries: list[dict[str, Any]],
    source_alerts: list[dict[str, Any]] | None = None,
    *,
    failed_count: int | None = None,
) -> list[dict[str, Any]]:
    """Add control-owned Audit alerts without exposing Audit payloads."""
    alerts: list[dict[str, Any]] = list(source_alerts or [])

    effective_failed_count = (
        sum(_audit_failed(entry) for entry in entries)
        if failed_count is None
        else max(0, failed_count)
    )
    if effective_failed_count:
        alerts.append(
            {
                "severity": "warning",
                "title": f"{effective_failed_count} recent MCP call failure(s)",
                "detail": "Open Audit for authorized call details",
            }
        )

    severity_rank = {"info": 0, "warning": 1, "critical": 2}
    alerts.sort(
        key=lambda alert: severity_rank.get(str(alert.get("severity")), 0),
        reverse=True,
    )
    return alerts[:_DASHBOARD_MAX_ALERTS]


def dashboard_snapshot(machine_snapshot: dict[str, Any]) -> dict[str, Any]:
    """Overlay control-owned Audit metadata onto executor machine telemetry."""
    raw_machine_alerts = machine_snapshot.get("alerts")
    source_alerts = (
        [dict(item) for item in raw_machine_alerts if isinstance(item, dict)]
        if isinstance(raw_machine_alerts, list)
        else []
    )
    try:
        audit_payload = query_audit(
            limit=_DASHBOARD_AUDIT_LIMIT,
            start_ts=time.time() - _DASHBOARD_WINDOW_S,
            sort="desc",
        )
        audit_source = "ok"
    except Exception as exc:
        audit_payload = {"entries": [], "count": 0, "total_matched": 0}
        audit_source = "degraded"
        source_alerts.append(
            {
                "severity": "warning",
                "title": "Audit activity unavailable",
                "detail": type(exc).__name__,
            }
        )

    raw_entries = audit_payload.get("entries")
    entries = (
        [entry for entry in raw_entries if isinstance(entry, dict)]
        if isinstance(raw_entries, list)
        else []
    )
    total_matched = audit_payload.get("total_matched", len(entries))
    if (
        isinstance(total_matched, bool)
        or not isinstance(total_matched, int)
        or total_matched < 0
    ):
        total_matched = len(entries)
    failed_count = audit_payload.get(
        "failed_matched", sum(_audit_failed(entry) for entry in entries)
    )
    if (
        isinstance(failed_count, bool)
        or not isinstance(failed_count, int)
        or failed_count < 0
        or failed_count > total_matched
    ):
        failed_count = sum(_audit_failed(entry) for entry in entries)
    alerts = dashboard_alerts(
        entries,
        source_alerts,
        failed_count=failed_count,
    )
    highest = str(alerts[0].get("severity") or "") if alerts else ""
    health = (
        "critical"
        if highest == "critical"
        else "attention"
        if alerts
        else "healthy"
    )

    raw_sources = machine_snapshot.get("sources")
    sources = dict(raw_sources) if isinstance(raw_sources, dict) else {}
    sources["audit"] = audit_source
    return {
        **machine_snapshot,
        "health": health,
        "alerts": alerts,
        "activity": dashboard_activity(entries),
        "audit_total_24h": total_matched,
        "audit_failed_24h": failed_count,
        "sources": sources,
    }
