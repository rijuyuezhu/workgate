"""Executor-owned machine telemetry for the WebUI dashboard."""

import math
import time
from typing import Any

from ..config.executor import ExecutorConfig
from ..telemetry.system import local_system_snapshot
from ..version import version_info
from .path import resolve_default_workdir

_MAX_ALERTS = 12


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    normalized = float(value)
    return normalized if math.isfinite(normalized) else None


def _machine_alerts(
    system: dict[str, Any], disk_path: str
) -> list[dict[str, Any]]:
    alerts: list[dict[str, Any]] = []
    for field, label, warning, critical in (
        ("disk_percent", "Disk", 85.0, 95.0),
        ("memory_percent", "Memory", 90.0, 98.0),
    ):
        percent = _finite_number(system.get(field))
        if percent is None or percent < warning:
            continue
        alerts.append(
            {
                "severity": "critical" if percent >= critical else "warning",
                "title": f"{label} is {percent:.0f}% full",
                "detail": (
                    disk_path
                    if field == "disk_percent"
                    else "Host memory pressure is elevated"
                ),
            }
        )
    return alerts[:_MAX_ALERTS]


def dashboard_snapshot(config: ExecutorConfig) -> dict[str, Any]:
    """Return machine-local dashboard data without control-owned Audit state."""
    disk_path = resolve_default_workdir(config.default_workdir)
    system = local_system_snapshot(disk_path)
    alerts = _machine_alerts(system, str(disk_path))
    highest = str(alerts[0].get("severity") or "") if alerts else ""
    health = (
        "critical"
        if highest == "critical"
        else "attention"
        if alerts
        else "healthy"
    )
    return {
        "generated_at": time.time(),
        "health": health,
        "version": version_info(),
        "system": system,
        "alerts": alerts,
        "sources": {"system": "ok"},
    }
