"""Integration tests for SDK-owned MCP session limits and idle expiration."""

import time

from fastapi.testclient import TestClient

from workgate.config.settings import clear_settings_cache
from workgate.control.mcp.app import build_mcp, build_mcp_http_app


def _initialize_payload() -> dict[str, object]:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "session-limit-test", "version": "1"},
        },
    }


def _mcp_headers(**extra: str) -> dict[str, str]:
    return {
        "accept": "application/json, text/event-stream",
        "content-type": "application/json",
        **extra,
    }


def test_stateful_mcp_sessions_have_idle_timeout_and_capacity_limit(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WORKGATE_DEFAULT_WORKDIR", str(tmp_path))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / ".state"))
    monkeypatch.setenv("WORKGATE_AUTH_MODE", "none")
    monkeypatch.setenv("WORKGATE_AGENT_BRIDGE_ENABLED", "false")
    monkeypatch.setenv("WORKGATE_MCP_SESSION_IDLE_TIMEOUT_S", "7")
    monkeypatch.setenv("WORKGATE_MCP_MAX_SESSIONS", "2")
    clear_settings_cache()

    mcp = build_mcp()
    app = build_mcp_http_app(mcp)
    manager = mcp.session_manager
    assert manager is not None
    assert manager.session_idle_timeout == 7
    assert manager.max_sessions == 2

    with TestClient(app, base_url="http://127.0.0.1") as client:
        first = client.post(
            "/mcp", json=_initialize_payload(), headers=_mcp_headers()
        )
        second = client.post(
            "/mcp", json=_initialize_payload(), headers=_mcp_headers()
        )
        rejected = client.post(
            "/mcp", json=_initialize_payload(), headers=_mcp_headers()
        )

        assert first.status_code == 200
        assert second.status_code == 200
        assert rejected.status_code == 503
        assert rejected.json()["error"]["message"] == "Too many open sessions"

        first_session = first.headers["mcp-session-id"]
        ping = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 2, "method": "ping"},
            headers=_mcp_headers(
                **{
                    "mcp-session-id": first_session,
                    "mcp-protocol-version": "2025-06-18",
                }
            ),
        )
        assert ping.status_code == 200

        deleted = client.delete(
            "/mcp",
            headers={
                "accept": "application/json",
                "mcp-session-id": first_session,
                "mcp-protocol-version": "2025-06-18",
            },
        )
        assert deleted.status_code == 200

        replacement = client.post(
            "/mcp", json=_initialize_payload(), headers=_mcp_headers()
        )
        assert replacement.status_code == 200


def test_idle_session_expiry_releases_capacity_without_delete(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WORKGATE_DEFAULT_WORKDIR", str(tmp_path))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / ".state"))
    monkeypatch.setenv("WORKGATE_AUTH_MODE", "none")
    monkeypatch.setenv("WORKGATE_AGENT_BRIDGE_ENABLED", "false")
    monkeypatch.setenv("WORKGATE_MCP_SESSION_IDLE_TIMEOUT_S", "1")
    monkeypatch.setenv("WORKGATE_MCP_MAX_SESSIONS", "1")
    clear_settings_cache()

    mcp = build_mcp()
    app = build_mcp_http_app(mcp)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        first = client.post(
            "/mcp", json=_initialize_payload(), headers=_mcp_headers()
        )
        blocked = client.post(
            "/mcp", json=_initialize_payload(), headers=_mcp_headers()
        )
        time.sleep(1.25)
        replacement = client.post(
            "/mcp", json=_initialize_payload(), headers=_mcp_headers()
        )

    assert first.status_code == 200
    assert blocked.status_code == 503
    assert replacement.status_code == 200
