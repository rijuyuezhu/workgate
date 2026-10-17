"""Committed-but-unacknowledged Executor writes must not leave MCP discovery stale."""

from dataclasses import replace
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient

from tests.support.paired import build_paired_http_app
from workgate.agent_bridge.auth import credential_store_key
from workgate.agent_bridge.auth_store import AgentAuthStore
from workgate.agent_bridge.state import load_agent_manifest
from workgate.config.settings import Settings
from workgate.control.executor_transport import ExecutorTransportError
from workgate.protocol.errors import ProtocolErrorCode

BASE = "https://workgate.example"


@pytest.mark.parametrize("kind", ["mcp", "auth"])
@pytest.mark.parametrize(
    "outcome", ["offered", "unknown", "queued", "malformed", "preoffer"]
)
def test_executor_remote_write_outcome_invalidates_only_when_necessary(
    tmp_path, kind, outcome
):
    workspace = tmp_path / "project"
    workspace.mkdir()
    app, harness = build_paired_http_app(
        Settings(
            default_workdir=workspace,
            state_dir=tmp_path / "state",
            auth_mode="none",
            base_url=BASE,
            agent_bridge_enabled=True,
        )
    )
    harness.control.config = replace(
        harness.control.config,
        agent_config_dir=tmp_path / "control-config",
        agent_auth_dir=tmp_path / "control-auth",
    )
    harness.executor.config = replace(
        harness.executor.config,
        agent_config_dir=tmp_path / "executor-config",
        agent_auth_dir=tmp_path / "executor-auth",
    )
    bridge = harness.control.agent_bridge
    bridge._settings = harness.control.config
    harness.executor.agent_bridge._config = harness.executor.config
    fetches = []

    def current_tool():
        manifest = load_agent_manifest(harness.executor.config.agent_config_dir)
        server = manifest.data.mcp_servers.get("uncertain")
        if kind == "mcp":
            return "added" if server is not None else None
        if server is None:
            return None
        identity = credential_store_key("uncertain", server)
        stored = AgentAuthStore(harness.executor.config.agent_auth_dir)
        return (
            "with_secret"
            if stored.list_secrets(identity).get(identity)
            else "without_secret"
        )

    async def fetch(_session_id, _server):
        fetches.append(1)
        tool = current_tool()
        return (
            [
                {
                    "server": "uncertain",
                    "tool": tool,
                    "description": "read current Executor state",
                    "input_schema": {},
                }
            ]
            if tool is not None
            else []
        )

    # Stub only external tool probing, not public router, HTTP, Executor RPC,
    # source manifest, auth storage or the actual discovery cache.
    bridge._discovery._fetch = fetch

    def search(client, session_id):
        response = client.post(
            "/tools/search_agent_mcp_tools",
            json={"session_id": session_id, "query": "uncertain"},
        )
        assert response.status_code == 200, response.text
        assert not response.json().get("isError"), response.text
        return [
            row["tool"] for row in response.json()["structuredContent"]["tools"]
        ]

    def mutation(client):
        if kind == "auth":
            return client.post(
                "/api/ui/integrations/auth",
                json={
                    "owner": "executor",
                    "executor_id": harness.executor_id,
                    "name": "uncertain",
                    "action": "set_secret",
                    "secret_name": "api_key",
                    "secret_value": "test-private-secret",
                },
            )
        return client.post(
            "/api/ui/integrations",
            json={
                "owner": "executor",
                "executor_id": harness.executor_id,
                "kind": "mcp",
                "action": "register",
                "name": "uncertain",
                "config": {"type": "stdio", "command": "echo"},
            },
        )

    with TestClient(app, base_url=BASE) as client:
        created = client.post(
            "/tools/session_start",
            json={
                "workdir": str(workspace),
                "executor_id": harness.executor_id,
            },
        )
        assert created.status_code == 200, created.text
        sid = created.json()["structuredContent"]["session_id"]
        if kind == "auth":
            register = client.post(
                "/api/ui/integrations",
                json={
                    "owner": "executor",
                    "executor_id": harness.executor_id,
                    "kind": "mcp",
                    "action": "register",
                    "name": "uncertain",
                    "config": {"type": "stdio", "command": "echo"},
                },
            )
            assert register.status_code == 200, register.text
        original = search(client, sid)
        assert len(fetches) == 1

        # A definite remote validation failure is not an uncertain write.
        if kind == "auth":
            rejected = client.post(
                "/api/ui/integrations/auth",
                json={
                    "owner": "executor",
                    "executor_id": harness.executor_id,
                    "name": "uncertain",
                    "action": "set_secret",
                    "secret_name": "",
                    "secret_value": "test-private-secret",
                },
            )
        else:
            rejected = client.post(
                "/api/ui/integrations",
                json={
                    "owner": "executor",
                    "executor_id": harness.executor_id,
                    "kind": "mcp",
                    "action": "register",
                    "name": "uncertain",
                    "config": {"type": "stdio", "command": ""},
                },
            )
        assert rejected.status_code == 409, rejected.text
        assert search(client, sid) == original
        assert len(fetches) == 1

        live_call = harness.control.executor_transport.call

        async def lost_reply(executor_id, op, args=None, **kwargs):
            if op == ("ui.mcp.auth" if kind == "auth" else "ui.mcp.manage"):
                if outcome == "preoffer":
                    raise ExecutorTransportError(
                        ProtocolErrorCode.EXECUTOR_OVERLOADED,
                        "rejected before offering to Executor",
                    )
                if outcome == "queued":
                    exc = TimeoutError("command never offered to Executor")
                    cast(Any, exc)._workgate_executor_delivery_state = "queued"
                    raise exc
                response = await live_call(executor_id, op, args, **kwargs)
                assert response.ok
                if outcome == "malformed":
                    return response.model_copy(
                        update={"result": "damaged response"}
                    )
                exc = TimeoutError("durable write committed; reply lost")
                if outcome == "offered":
                    cast(Any, exc)._workgate_executor_delivery_state = "offered"
                raise exc
            return await live_call(executor_id, op, args, **kwargs)

        harness.control.executor_transport.call = lost_reply
        result = mutation(client)
        assert result.status_code == 503, result.text
        assert result.json()["error"] == (
            "AuthUnavailable" if kind == "auth" else "ManagementUnavailable"
        )
        changed = outcome not in {"queued", "preoffer"}
        assert (
            current_tool() != (original[0] if original else None)
        ) == changed
        visible = search(client, sid)
        assert visible == (
            [current_tool()] if current_tool() is not None else []
        )
        assert len(fetches) == (2 if changed else 1)
