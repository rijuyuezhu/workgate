"""WebUI MCP writes invalidate the *same* discovery used by public tools."""

from dataclasses import replace

from fastapi.testclient import TestClient

from tests.support.paired import build_paired_http_app
from workgate.config.settings import Settings

BASE = "https://workgate.example"


def test_public_discovery_refreshes_after_webui_mcp_management(tmp_path):
    workspace = tmp_path / "workspace"
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
        harness.control.config, agent_config_dir=tmp_path / "control-agent"
    )
    harness.executor.config = replace(
        harness.executor.config, agent_config_dir=tmp_path / "executor-agent"
    )
    bridge = harness.control.agent_bridge
    bridge._settings = harness.control.config
    harness.executor.agent_bridge._config = harness.executor.config
    calls = []

    async def discovery_rows(session_id, server):
        calls.append((session_id, server))
        names = ("shared", "shift")
        rows = []
        for name in names:
            if server is not None and server != name:
                continue
            _, owner = await bridge._resolve_server_owner(name, session_id)
            if owner == "unknown":
                continue
            rows.append(
                {
                    "server": name,
                    "tool": f"{owner}_tool",
                    "description": f"{name} on {owner}",
                    "input_schema": {"type": "object"},
                }
            )
        return rows

    # Only tool probing is simulated. The public tool router, paired Executor,
    # owner selection, real WebUI HTTP writes and discovery cache are real.
    bridge._discovery._fetch = discovery_rows

    def write(client, owner, action, name, **fields):
        return client.post(
            "/api/ui/integrations",
            json={
                "kind": "mcp",
                "owner": owner,
                "executor_id": harness.executor_id,
                "action": action,
                "name": name,
                **fields,
            },
        )

    def get(client, owner, name):
        result = client.get(
            "/api/ui/integrations",
            params={
                "kind": "mcp",
                "owner": owner,
                "executor_id": harness.executor_id,
                "action": "get",
                "name": name,
            },
        )
        assert result.status_code == 200, result.text
        return result.json()["data"]["server"]

    def search(client, session_id, name):
        result = client.post(
            "/tools/search_agent_mcp_tools",
            json={"session_id": session_id, "query": name},
        )
        assert result.status_code == 200, result.text
        payload = result.json()
        assert not payload.get("isError"), payload
        return [row["tool"] for row in payload["structuredContent"]["tools"]]

    def inspect(client, session_id, name, tool):
        result = client.post(
            "/tools/inspect_agent_mcp_tool",
            json={"session_id": session_id, "server": name, "tool": tool},
        )
        assert result.status_code == 200, result.text
        assert not result.json().get("isError"), result.text
        return result.json()["structuredContent"]["tool"]

    with TestClient(app, base_url=BASE) as client:
        started = client.post(
            "/tools/session_start",
            json={
                "workdir": str(workspace),
                "executor_id": harness.executor_id,
            },
        )
        assert started.status_code == 200, started.text
        sid = started.json()["structuredContent"]["session_id"]
        assert (
            write(
                client,
                "control",
                "register",
                "shared",
                config={
                    "type": "http",
                    "url": "https://example.org/mcp",
                },
            ).status_code
            == 200
        )
        assert search(client, sid, "shared") == ["control_tool"]
        assert inspect(client, sid, "shared", "control_tool") == "control_tool"
        assert search(client, sid, "shared") == ["control_tool"]
        # Search then inspect of the same full catalog should reuse one cached read.
        first_count = len(calls)
        assert first_count == 1, calls

        get(client, "control", "shared")
        assert (
            write(
                client,
                "control",
                "disable",
                "shared",
                expected_revision="a" * 64,
            ).status_code
            == 409
        )
        assert search(client, sid, "shared") == ["control_tool"]
        assert (
            len(calls) == first_count
        )  # Reads and rejected CAS must not clear.

        # A successful Skill write is unrelated to the MCP metadata cache.
        skill = client.post(
            "/api/ui/integrations",
            json={
                "kind": "skill",
                "owner": "control",
                "action": "create",
                "name": "unrelated",
                "content": "# Unrelated Skill\n",
            },
        )
        assert skill.status_code == 200, skill.text
        assert search(client, sid, "shared") == ["control_tool"]
        assert len(calls) == first_count

        assert (
            write(
                client,
                "executor",
                "register",
                "shared",
                config={
                    "type": "stdio",
                    "command": "echo",
                },
            ).status_code
            == 200
        )
        assert search(client, sid, "shared") == ["executor_tool"]
        assert (
            inspect(client, sid, "shared", "executor_tool") == "executor_tool"
        )

        revision = get(client, "executor", "shared")["revision"]
        assert (
            write(
                client,
                "executor",
                "disable",
                "shared",
                expected_revision=revision,
            ).status_code
            == 200
        )
        assert search(client, sid, "shared") == ["control_tool"]
        revision = get(client, "executor", "shared")["revision"]
        assert (
            write(
                client,
                "executor",
                "enable",
                "shared",
                expected_revision=revision,
            ).status_code
            == 200
        )
        assert search(client, sid, "shared") == ["executor_tool"]

        # Auth metadata is read-only; actual secret changes invalidate discovery.
        auth_url = "/api/ui/integrations/auth?owner=control&name=shared"
        assert client.get(auth_url).status_code == 200
        before_auth = len(calls)
        assert search(client, sid, "shared") == ["executor_tool"]
        assert len(calls) == before_auth
        assert (
            client.post(
                "/api/ui/integrations/auth",
                json={
                    "owner": "control",
                    "name": "shared",
                    "action": "set_secret",
                    "secret_name": "api_key",
                    "secret_value": "test-private-value",
                },
            ).status_code
            == 200
        )
        assert search(client, sid, "shared") == ["executor_tool"]
        assert len(calls) == before_auth + 1
        assert (
            client.post(
                "/api/ui/integrations/auth",
                json={
                    "owner": "control",
                    "name": "shared",
                    "action": "delete_secret",
                    "secret_name": "not_stored",
                },
            ).json()["data"]["changed"]
            is False
        )
        assert search(client, sid, "shared") == ["executor_tool"]
        assert len(calls) == before_auth + 1

        # Move between hosts, including source removal, immediately refreshes.
        assert (
            write(
                client,
                "control",
                "register",
                "shift",
                config={
                    "type": "http",
                    "url": "https://example.org/shift",
                },
            ).status_code
            == 200
        )
        assert search(client, sid, "shift") == ["control_tool"]
        moved = client.post(
            "/api/ui/integrations/move",
            json={
                "owner": "control",
                "executor_id": harness.executor_id,
                "destination_owner": "executor",
                "destination_executor_id": harness.executor_id,
                "name": "shift",
                "expected_revision": get(client, "control", "shift")[
                    "revision"
                ],
                "config": {"type": "stdio", "command": "echo"},
            },
        )
        assert moved.status_code == 200, moved.text
        assert search(client, sid, "shift") == ["executor_tool"]
        assert inspect(client, sid, "shift", "executor_tool") == "executor_tool"
        client.post("/tools/session_end", json={"session_id": sid})
