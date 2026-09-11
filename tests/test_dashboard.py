import workgate.ui.dashboard as dashboard_module


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
    monkeypatch,
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
