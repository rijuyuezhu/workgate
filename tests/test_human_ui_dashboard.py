import copy
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import workgate.ui.dashboard as dashboard_module
import workgate.ui.http.dashboard as ui_dashboard_module
from tests.helpers import build_paired_control_harness, build_paired_http_app
from workgate.config.settings import clear_settings_cache, get_settings
from workgate.oauth.core.scopes import SCOPE_SHELL_READ
from workgate.oauth.protocol.token_codec import issue_access_token

BASE_URL = "https://workgate.example"


@pytest.fixture(autouse=True)
def _reset_settings():
    clear_settings_cache()
    yield
    clear_settings_cache()


def _configure(
    monkeypatch: pytest.MonkeyPatch,
    workspace: Path,
    *,
    auth_mode: str = "none",
) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(workspace))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(workspace / ".state"))
    monkeypatch.setenv("WORKGATE_AUTH_MODE", auth_mode)
    monkeypatch.setenv("WORKGATE_BASE_URL", BASE_URL)
    monkeypatch.setenv("WORKGATE_AGENT_BRIDGE_ENABLED", "false")
    clear_settings_cache()


def _client(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    auth_mode: str = "none",
):
    _configure(monkeypatch, tmp_path / "workspace", auth_mode=auth_mode)
    app, harness = build_paired_http_app(get_settings())
    return (
        TestClient(
            app,
            base_url=BASE_URL,
            client=("203.0.113.14", 50005),
        ),
        harness,
    )


def _token(scope: str) -> str:
    return issue_access_token(
        client_id="webui-dashboard-test",
        scope=scope,
        resource=f"{BASE_URL}/mcp",
    )


def _headers(scope: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {_token(scope)}"}


def _snapshot(*, health: str = "healthy") -> dict[str, Any]:
    return {
        "generated_at": 100.0,
        "health": health,
        "version": {
            "version": "3.9.1",
            "package_version": "3.9.1",
            "python": "3.14.0",
            "platform": "test-platform",
        },
        "system": {
            "timestamp": 99.0,
            "cpu_percent": 12.5,
            "cpu_count": 8,
            "memory_percent": 45.0,
            "memory_used_bytes": 450,
            "memory_total_bytes": 1_000,
            "disk_percent": 60.0,
            "disk_used_bytes": 600,
            "disk_total_bytes": 1_000,
            "load_1m": 1.5,
            "network_rx_bps": 100.0,
            "network_tx_bps": 50.0,
            "uptime_s": 3_600.0,
        },
        "alerts": [],
        "activity": [
            {
                "timestamp": 98.0,
                "kind": "success",
                "title": "read",
                "detail": "files",
                "duration_ms": 4.0,
            }
        ],
        "audit_total_24h": 3,
        "audit_failed_24h": 0,
        "sources": {"system": "ok", "audit": "ok"},
    }


def test_dashboard_routes_to_explicit_executor_and_normalizes_snapshot(
    monkeypatch, tmp_path
):
    client, harness = _client(monkeypatch, tmp_path)
    monkeypatch.setattr(dashboard_module, "dashboard_snapshot", _snapshot)

    response = client.get(
        "/api/ui/dashboard", params={"executor_id": harness.executor_id}
    )

    assert response.status_code == 200
    payload = response.json()["data"]
    assert payload["executor_id"] == harness.executor_id
    assert payload["health"] == "healthy"
    assert payload["system"]["cpu_percent"] == 12.5
    assert payload["activity"] == [
        {
            "timestamp": 98.0,
            "kind": "success",
            "title": "read",
            "detail": "files",
            "duration_ms": 4.0,
            "node": harness.executor_id,
        }
    ]


def test_dashboard_enforces_shell_read_scope(monkeypatch, tmp_path):
    client, harness = _client(monkeypatch, tmp_path, auth_mode="oauth")
    monkeypatch.setattr(dashboard_module, "dashboard_snapshot", _snapshot)

    denied = client.get(
        "/api/ui/dashboard",
        params={"executor_id": harness.executor_id},
        headers=_headers("shell:execute"),
    )
    allowed = client.get(
        "/api/ui/dashboard",
        params={"executor_id": harness.executor_id},
        headers=_headers(SCOPE_SHELL_READ),
    )

    assert denied.status_code == 403
    assert SCOPE_SHELL_READ in denied.text
    assert allowed.status_code == 200


def test_dashboard_handles_executor_unavailable_and_malformed_snapshot(
    monkeypatch, tmp_path
):
    client, harness = _client(monkeypatch, tmp_path)

    malformed = _snapshot()
    malformed["system"] = "bad"
    monkeypatch.setattr(
        dashboard_module, "dashboard_snapshot", lambda: malformed
    )
    bad = client.get(
        "/api/ui/dashboard", params={"executor_id": harness.executor_id}
    )
    assert bad.status_code == 502
    assert "malformed dashboard system data" in bad.text

    async def offline(*args, **kwargs):
        raise ConnectionError("executor unavailable")

    monkeypatch.setattr(harness.control.executor_transport, "call", offline)
    unavailable = client.get(
        "/api/ui/dashboard", params={"executor_id": harness.executor_id}
    )
    assert unavailable.status_code == 503


def test_dashboard_snapshot_rejects_out_of_range_and_oversized_executor_values():
    cases: list[tuple[str, Any, str]] = [
        ("cpu_percent", -1.0, "cpu_percent"),
        ("memory_percent", 101.0, "memory_percent"),
        ("network_rx_bps", -1.0, "network_rx_bps"),
    ]
    for field, value, message in cases:
        snapshot = copy.deepcopy(_snapshot())
        snapshot["system"][field] = value
        with pytest.raises(RuntimeError, match=message):
            ui_dashboard_module._normalize_snapshot("exec_123", snapshot)

    invalid_counts = copy.deepcopy(_snapshot())
    invalid_counts["audit_total_24h"] = 1
    invalid_counts["audit_failed_24h"] = 2
    with pytest.raises(RuntimeError, match="Audit counts"):
        ui_dashboard_module._normalize_snapshot("exec_123", invalid_counts)

    invalid_source = copy.deepcopy(_snapshot())
    invalid_source["sources"]["audit"] = "unknown"
    with pytest.raises(RuntimeError, match="source states"):
        ui_dashboard_module._normalize_snapshot("exec_123", invalid_source)

    oversized = copy.deepcopy(_snapshot())
    oversized["alerts"] = [
        {"severity": "warning", "title": "x" * 1_025, "detail": "bounded"}
    ]
    with pytest.raises(RuntimeError, match="alert title"):
        ui_dashboard_module._normalize_snapshot("exec_123", oversized)


@pytest.mark.asyncio
async def test_executor_dashboard_dispatch_is_native_and_sessionless(
    monkeypatch, tmp_path
):
    _configure(monkeypatch, tmp_path / "workspace")
    harness = build_paired_control_harness(get_settings())
    monkeypatch.setattr(dashboard_module, "dashboard_snapshot", _snapshot)

    result = await harness.executor.dispatcher.execute("dashboard_snapshot", {})

    assert result["health"] == "healthy"
    assert result["system"]["cpu_count"] == 8
