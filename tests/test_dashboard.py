import pytest

import workgate.executor.dashboard as executor_dashboard_module
import workgate.ui.dashboard as dashboard_module
from workgate.config.settings import Settings
from workgate.executor.config import resolve_executor_config


def _machine_snapshot() -> dict:
    return {
        "generated_at": 20.0,
        "health": "healthy",
        "version": {
            "version": "9.9.9",
            "package_version": "9.9.9",
            "python": "3.14",
            "platform": "test-platform",
        },
        "system": {
            "timestamp": 20.0,
            "cpu_percent": 10.0,
            "cpu_count": 4,
            "memory_percent": 20.0,
            "memory_used_bytes": 200,
            "memory_total_bytes": 1_000,
            "disk_percent": 30.0,
            "disk_used_bytes": 300,
            "disk_total_bytes": 1_000,
            "load_1m": 0.5,
            "network_rx_bps": 12.0,
            "network_tx_bps": 8.0,
            "uptime_s": 100.0,
        },
        "alerts": [],
        "sources": {"system": "ok"},
    }


def test_dashboard_snapshot_exposes_metadata_only_and_failure_alert(
    monkeypatch,
) -> None:
    monkeypatch.setattr(dashboard_module.time, "time", lambda: 100.0)
    monkeypatch.setattr(
        dashboard_module,
        "query_audit",
        lambda **kwargs: {
            "entries": [
                {
                    "ts": 99.0,
                    "tool": "write_file",
                    "operation": "files",
                    "status": "failed",
                    "ok": False,
                    "paired": True,
                    "duration_ms": 5,
                    "input": {"path": "secret.txt", "content": "hidden"},
                    "output": {"content": "hidden"},
                    "error": {"message": "hidden"},
                },
                {
                    "ts": 98.0,
                    "tool": "read",
                    "operation": "files",
                    "status": "success",
                    "ok": True,
                    "paired": True,
                    "duration_ms": 2,
                },
            ],
            "count": 2,
            "total_matched": 7,
            "failed_matched": 4,
        },
    )

    snapshot = dashboard_module.dashboard_snapshot(_machine_snapshot())

    assert snapshot["health"] == "attention"
    assert snapshot["version"]["version"] == "9.9.9"
    assert snapshot["audit_total_24h"] == 7
    assert snapshot["audit_failed_24h"] == 4
    assert snapshot["sources"] == {"system": "ok", "audit": "ok"}
    assert snapshot["alerts"][0]["title"] == "4 recent MCP call failure(s)"
    assert snapshot["activity"][0] == {
        "timestamp": 99.0,
        "kind": "failed",
        "title": "write_file",
        "detail": "files",
        "duration_ms": 5.0,
    }
    assert not ({"input", "output", "error"} & snapshot["activity"][0].keys())


def test_dashboard_snapshot_preserves_machine_alerts(monkeypatch) -> None:
    machine = _machine_snapshot()
    machine["alerts"] = [
        {
            "severity": "critical",
            "title": "Workspace disk is 99% full",
            "detail": "/executor/workspace",
        }
    ]
    monkeypatch.setattr(
        dashboard_module,
        "query_audit",
        lambda **kwargs: {
            "entries": [],
            "count": 0,
            "total_matched": 0,
            "failed_matched": 0,
        },
    )

    snapshot = dashboard_module.dashboard_snapshot(machine)

    assert snapshot["health"] == "critical"
    assert snapshot["alerts"] == machine["alerts"]


def test_dashboard_snapshot_degrades_when_audit_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        dashboard_module,
        "query_audit",
        lambda **kwargs: (_ for _ in ()).throw(
            OSError("audit store unavailable")
        ),
    )

    snapshot = dashboard_module.dashboard_snapshot(_machine_snapshot())

    assert snapshot["health"] == "attention"
    assert snapshot["sources"]["audit"] == "degraded"
    assert snapshot["audit_total_24h"] == 0
    assert snapshot["activity"] == []
    assert snapshot["alerts"][0]["title"] == "Audit activity unavailable"


def test_executor_dashboard_snapshot_health_and_machine_alerts(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    config = resolve_executor_config(
        Settings(
            workspace_root=tmp_path / "workspace", state_dir=tmp_path / "state"
        )
    )
    system = {"disk_percent": 90.0, "memory_percent": 20.0}
    monkeypatch.setattr(
        executor_dashboard_module,
        "local_system_snapshot",
        lambda root: dict(system),
    )
    monkeypatch.setattr(executor_dashboard_module.time, "time", lambda: 123.0)
    monkeypatch.setattr(
        executor_dashboard_module,
        "version_info",
        lambda: {"version": "test"},
    )

    snapshot = executor_dashboard_module.dashboard_snapshot(config)

    assert snapshot == {
        "generated_at": 123.0,
        "health": "attention",
        "version": {"version": "test"},
        "system": system,
        "alerts": [
            {
                "severity": "warning",
                "title": "Workspace disk is 90% full",
                "detail": str(config.workspace_root),
            }
        ],
        "sources": {"system": "ok"},
    }

    system.update({"disk_percent": 96.0, "memory_percent": 99.0})
    critical = executor_dashboard_module.dashboard_snapshot(config)
    assert critical["health"] == "critical"
    assert critical["alerts"][0]["severity"] == "critical"
    assert critical["alerts"][1] == {
        "severity": "critical",
        "title": "Memory is 99% full",
        "detail": "Host memory pressure is elevated",
    }


@pytest.mark.parametrize("value", [True, "90", float("nan"), None])
def test_executor_dashboard_ignores_non_finite_machine_metrics(value) -> None:
    assert executor_dashboard_module._finite_number(value) is None
    assert (
        executor_dashboard_module._machine_alerts(
            {"disk_percent": value, "memory_percent": value}, "/workspace"
        )
        == []
    )


def test_dashboard_snapshot_normalizes_invalid_audit_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        dashboard_module,
        "query_audit",
        lambda **kwargs: {
            "entries": [
                {
                    "ts": 1.0,
                    "tool": "read",
                    "status": "failed",
                    "ok": False,
                    "duration_ms": True,
                }
            ],
            "total_matched": True,
            "failed_matched": 99,
        },
    )

    snapshot = dashboard_module.dashboard_snapshot(_machine_snapshot())

    assert snapshot["audit_total_24h"] == 1
    assert snapshot["audit_failed_24h"] == 1
    assert snapshot["activity"][0]["duration_ms"] is None
