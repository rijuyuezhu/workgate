import copy
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import workgate.ui.dashboard as dashboard_module
import workgate.ui.http.common as ui_common_module
import workgate.ui.http.dashboard as ui_dashboard_module
from workgate.config.settings import clear_settings_cache
from workgate.control.http.app import build_http_app
from workgate.oauth.core.scopes import SCOPE_REMOTE_USE, SCOPE_SHELL_READ
from workgate.oauth.protocol.token_codec import issue_access_token
from workgate.remote_worker.dispatch import execute_worker_tool
from workgate.schemas.result_models.remote import (
    RemoteListMachinesOutput,
    RemoteMachineInfo,
)

BASE_URL = "https://workgate.example"


@pytest.fixture(autouse=True)
def _reset_settings():
    clear_settings_cache()
    yield
    clear_settings_cache()


def _configure(
    monkeypatch,
    workspace: Path,
    *,
    auth_mode: str = "none",
    remote_enabled: bool = False,
) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(workspace))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(workspace / ".state"))
    monkeypatch.setenv("WORKGATE_AUTH_MODE", auth_mode)
    monkeypatch.setenv("WORKGATE_BASE_URL", BASE_URL)
    monkeypatch.setenv("WORKGATE_AGENT_BRIDGE_ENABLED", "false")
    monkeypatch.setenv(
        "WORKGATE_REMOTE_ENABLED", "true" if remote_enabled else "false"
    )
    clear_settings_cache()


def _client(
    monkeypatch,
    tmp_path: Path,
    *,
    auth_mode: str = "none",
    remote_enabled: bool = False,
) -> TestClient:
    _configure(
        monkeypatch,
        tmp_path / "workspace",
        auth_mode=auth_mode,
        remote_enabled=remote_enabled,
    )
    return TestClient(
        build_http_app(),
        base_url=BASE_URL,
        client=("203.0.113.14", 50005),
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


def test_local_dashboard_returns_normalized_process_snapshot(
    monkeypatch, tmp_path
):
    client = _client(monkeypatch, tmp_path)
    monkeypatch.setattr(ui_dashboard_module, "dashboard_snapshot", _snapshot)

    response = client.get("/api/ui/dashboard", params={"machine": "local"})

    assert response.status_code == 200
    payload = response.json()["data"]
    assert payload["machine"] == "local"
    assert payload["remote"] is False
    assert payload["health"] == "healthy"
    assert payload["system"]["cpu_percent"] == 12.5
    assert payload["activity"] == [
        {
            "timestamp": 98.0,
            "kind": "success",
            "title": "read",
            "detail": "files",
            "duration_ms": 4.0,
            "node": "local",
        }
    ]


class _FakeManager:
    def __init__(self, status: str = "online") -> None:
        self.status = status

    def list_machines(self) -> RemoteListMachinesOutput:
        return RemoteListMachinesOutput(
            machines=[
                RemoteMachineInfo(
                    name="edge",
                    status=self.status,
                    workdir="/srv/workspace",
                    last_seen=1.0,
                    queue_depth=0,
                    capabilities=["dashboard"],
                    info={},
                )
            ],
            counts={self.status: 1, "total": 1},
        )


class _FakeRemoteDashboard:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any], int]] = []
        self.malformed = False

    async def call(
        self,
        machine: str,
        tool: str,
        args: dict[str, Any],
        timeout_s: int,
    ) -> dict[str, Any]:
        assert machine == "edge"
        assert tool == "dashboard_snapshot"
        assert args == {}
        assert "session_id" not in args
        assert 1 <= timeout_s <= 60
        self.calls.append((machine, tool, dict(args), timeout_s))
        if self.malformed:
            malformed = _snapshot()
            malformed["system"] = "bad"
            return {"ok": True, "data": malformed}
        return {"ok": True, "data": _snapshot(health="attention")}


def _remote_client(
    monkeypatch,
    tmp_path: Path,
    fake: _FakeRemoteDashboard,
    *,
    auth_mode: str = "none",
    status: str = "online",
) -> TestClient:
    client = _client(
        monkeypatch,
        tmp_path,
        auth_mode=auth_mode,
        remote_enabled=True,
    )
    monkeypatch.setattr(
        ui_common_module,
        "remote_manager",
        lambda: _FakeManager(status=status),
    )
    monkeypatch.setattr(
        ui_dashboard_module, "call_remote_worker_tool", fake.call
    )
    return client


def test_remote_dashboard_uses_process_scoped_worker_rpc(monkeypatch, tmp_path):
    fake = _FakeRemoteDashboard()
    client = _remote_client(monkeypatch, tmp_path, fake)

    response = client.get("/api/ui/dashboard", params={"machine": "edge"})

    assert response.status_code == 200
    payload = response.json()["data"]
    assert payload["machine"] == "edge"
    assert payload["remote"] is True
    assert payload["health"] == "attention"
    assert payload["activity"][0]["node"] == "edge"
    assert len(fake.calls) == 1
    machine, tool, args, timeout_s = fake.calls[0]
    assert (machine, tool, args) == ("edge", "dashboard_snapshot", {})
    assert 1 <= timeout_s <= 60


def test_remote_dashboard_enforces_scopes_and_handles_offline_and_malformed(
    monkeypatch, tmp_path
):
    fake = _FakeRemoteDashboard()
    client = _remote_client(
        monkeypatch,
        tmp_path,
        fake,
        auth_mode="oauth",
    )
    read_only = _headers(SCOPE_SHELL_READ)
    remote_read = _headers(f"{SCOPE_SHELL_READ} {SCOPE_REMOTE_USE}")

    denied = client.get(
        "/api/ui/dashboard",
        params={"machine": "edge"},
        headers=read_only,
    )
    allowed = client.get(
        "/api/ui/dashboard",
        params={"machine": "edge"},
        headers=remote_read,
    )

    assert denied.status_code == 403
    assert SCOPE_REMOTE_USE in denied.text
    assert allowed.status_code == 200

    fake.malformed = True
    malformed = client.get(
        "/api/ui/dashboard",
        params={"machine": "edge"},
        headers=remote_read,
    )
    assert malformed.status_code == 502
    assert "malformed dashboard system data" in malformed.text

    offline_fake = _FakeRemoteDashboard()
    offline_client = _remote_client(
        monkeypatch,
        tmp_path / "offline",
        offline_fake,
        auth_mode="oauth",
        status="offline",
    )
    offline_remote_read = _headers(f"{SCOPE_SHELL_READ} {SCOPE_REMOTE_USE}")
    offline = offline_client.get(
        "/api/ui/dashboard",
        params={"machine": "edge"},
        headers=offline_remote_read,
    )
    assert offline.status_code == 503
    assert offline_fake.calls == []


def test_dashboard_snapshot_rejects_out_of_range_and_oversized_worker_values():
    cases: list[tuple[str, Any, str]] = [
        ("cpu_percent", -1.0, "cpu_percent"),
        ("memory_percent", 101.0, "memory_percent"),
        ("network_rx_bps", -1.0, "network_rx_bps"),
    ]
    for field, value, message in cases:
        snapshot = copy.deepcopy(_snapshot())
        snapshot["system"][field] = value
        with pytest.raises(RuntimeError, match=message):
            ui_dashboard_module._normalize_snapshot("edge", snapshot)

    invalid_counts = copy.deepcopy(_snapshot())
    invalid_counts["audit_total_24h"] = 1
    invalid_counts["audit_failed_24h"] = 2
    with pytest.raises(RuntimeError, match="Audit counts"):
        ui_dashboard_module._normalize_snapshot("edge", invalid_counts)

    invalid_source = copy.deepcopy(_snapshot())
    invalid_source["sources"]["audit"] = "unknown"
    with pytest.raises(RuntimeError, match="source states"):
        ui_dashboard_module._normalize_snapshot("edge", invalid_source)

    oversized = copy.deepcopy(_snapshot())
    oversized["alerts"] = [
        {"severity": "warning", "title": "x" * 1_025, "detail": "bounded"}
    ]
    with pytest.raises(RuntimeError, match="alert title"):
        ui_dashboard_module._normalize_snapshot("edge", oversized)


@pytest.mark.asyncio
async def test_worker_dashboard_dispatch_is_native_and_sessionless(monkeypatch):
    monkeypatch.setattr(dashboard_module, "dashboard_snapshot", _snapshot)

    result = await execute_worker_tool("dashboard_snapshot", {})

    assert result["health"] == "healthy"
    assert result["system"]["cpu_count"] == 8
