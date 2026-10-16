"""On-demand Executor detail projection: privilege, redaction and offline behavior."""

import json
from typing import Literal

from fastapi.testclient import TestClient

from tests.support.paired import build_paired_http_app
from workgate.agent_bridge.diagnostics import manifest_diagnostics
from workgate.config.role_config import use_role_config
from workgate.config.settings import Settings
from workgate.oauth.core.scopes import SCOPE_EXECUTOR_USE, SCOPE_SHELL_READ
from workgate.oauth.core.state import OAuthState, use_oauth_state
from workgate.oauth.protocol.token_codec import issue_access_token
from workgate.protocol.executor import ExecutorResult
from workgate.protocol.ids import new_command_id

BASE = "https://workgate.example"


def harness_for(tmp_path, *, auth: Literal["none", "oauth"] = "none"):
    (tmp_path / "project").mkdir(exist_ok=True)
    settings = Settings(
        default_workdir=tmp_path / "project",
        state_dir=tmp_path / "state",
        auth_mode=auth,
        base_url=BASE,
        agent_bridge_enabled=True,
    )
    app, harness = build_paired_http_app(settings)
    return TestClient(app, base_url=BASE), harness


def test_manifest_diagnostics_never_returns_credentials_or_paths(tmp_path):
    source = tmp_path / "private-agent"
    source.mkdir(mode=0o700)
    (source / "config.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "local-stdio": {
                        "type": "stdio",
                        "command": "super-secret-executable",
                        "args": ["HIDDEN_ARGUMENT"],
                    },
                    "control-http": {
                        "type": "http",
                        "url": "https://TOKEN_VALUE.example/private",
                    },
                },
            }
        )
    )
    control = manifest_diagnostics(source, owner="control")
    local = manifest_diagnostics(source, owner="executor")
    assert [item["name"] for item in control["servers"]] == ["control-http"]
    assert [item["name"] for item in local["servers"]] == ["local-stdio"]
    for output in (control, local):
        serialized = json.dumps(output)
        assert "TOKEN_VALUE" not in serialized
        assert "HIDDEN_ARGUMENT" not in serialized
        assert "super-secret-executable" not in serialized
        assert str(tmp_path) not in serialized

    # Syntax and validation exceptions must not echo user-supplied content.
    (source / "config.json").write_text('{"top-secret": "private-PASSWORD"')
    invalid = manifest_diagnostics(source, owner="executor")
    assert invalid["status"] == "invalid_config"
    assert invalid["errors"] > 0
    assert "private-PASSWORD" not in json.dumps(invalid)


def test_diagnostic_reads_never_chmod_or_follow_symlink(tmp_path):
    import stat

    source = tmp_path / "inspection"
    source.mkdir(mode=0o750)
    (source / "config.json").write_text('{"version":1,"mcpServers":{}}')
    before = stat.S_IMODE(source.stat().st_mode)
    assert manifest_diagnostics(source, owner="control")["status"] == "loaded"
    assert stat.S_IMODE(source.stat().st_mode) == before
    secret_file = tmp_path / "sensitive.txt"
    secret_file.write_text("PRIVATE_VALUE")
    (source / "config.json").unlink()
    (source / "config.json").symlink_to(secret_file)
    result = manifest_diagnostics(source, owner="executor")
    assert result["status"] == "invalid_config"
    assert "PRIVATE_VALUE" not in json.dumps(result)
    (source / "config.json").unlink()
    (source / "config.json").write_bytes(b"x" * 1_048_577)
    assert (
        manifest_diagnostics(source, owner="control")["status"] == "unavailable"
    )


def test_real_executor_diagnostics_is_bounded_separate_and_read_only(tmp_path):
    client, harness = harness_for(tmp_path)
    with client:
        for _ in range(2):
            inventory = client.get("/api/ui/executors")
            assert inventory.status_code == 200
            assert "diagnostics" not in inventory.text
        response = client.get(
            "/api/ui/executors/diagnostics",
            params={"executor_id": harness.executor_id},
        )
        assert response.status_code == 200, response.text
        data = response.json()["data"]
        assert data["executor_id"] == harness.executor_id
        assert data["status"] == "available"
        assert data["control_mcp"]["enabled"] is True
        assert "agent" not in data["executor"]
        assert "health" not in data["executor"]
        assert data["executor"]["bridge"]["status"] in {
            "loaded",
            "missing_config",
            "invalid_config",
        }
        assert len(response.content) < 16_384
        assert not any(
            token in response.text
            for token in (
                '"env"',
                '"headers"',
                '"command"',
                '"url"',
                '"args"',
                '"source_path"',
            )
        )


def test_offline_executor_is_not_probed_and_control_status_remains(
    tmp_path, monkeypatch
):
    client, harness = harness_for(tmp_path)

    async def offline(_id):
        return False

    async def no_rpc(*_args, **_kwargs):
        raise AssertionError("offline Executor should not be probed")

    monkeypatch.setattr(
        harness.control.executor_transport, "is_online", offline
    )
    monkeypatch.setattr(harness.control.executor_transport, "call", no_rpc)
    with client:
        response = client.get(
            "/api/ui/executors/diagnostics",
            params={"executor_id": harness.executor_id},
        )
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["status"] == "offline"
        assert data["executor"] is None
        assert "status" in data["control_mcp"]
        assert (
            client.get(
                "/api/ui/executors/diagnostics",
                params={"executor_id": "missing"},
            ).status_code
            == 404
        )
        assert client.get("/api/ui/executors/diagnostics").status_code == 400


def test_untrusted_executor_payload_cannot_inject_secrets(
    tmp_path, monkeypatch
):
    client, harness = harness_for(tmp_path)
    secret = "SECRET_MUST_NOT_LEAK"

    async def malicious(_id, _op, _args, *, timeout_s):
        assert timeout_s == 8
        return ExecutorResult(
            id=new_command_id(),
            ok=True,
            result={
                "env": {"API_TOKEN": secret},
                "bridge": {
                    "status": "loaded",
                    "servers": [
                        {
                            "name": "stdio-safe",
                            "type": "stdio",
                            "enabled": True,
                            "url": f"https://{secret}.example",
                            "headers": {"Authorization": secret},
                        }
                    ],
                },
                "skills": {
                    "enabled": True,
                    "selected": [
                        {
                            "name": "skill-safe",
                            "source": "managed",
                            "source_path": secret,
                        }
                    ],
                },
                "health": {"cpu_percent": 12.3, "extra": secret},
            },
        )

    monkeypatch.setattr(harness.control.executor_transport, "call", malicious)
    with client:
        result = client.get(
            "/api/ui/executors/diagnostics",
            params={"executor_id": harness.executor_id},
        )
        assert result.status_code == 200
        assert secret not in result.text
        data = result.json()["data"]["executor"]
        assert data["bridge"]["servers"][0] == {
            "name": "stdio-safe",
            "type": "stdio",
            "enabled": True,
        }
        assert data["skills"]["selected"][0] == {
            "name": "skill-safe",
            "source": "managed",
        }
        assert "health" not in data


def test_malformed_peer_diagnostics_fail_closed(tmp_path, monkeypatch):
    client, harness = harness_for(tmp_path)

    async def invalid(_id, _op, _args, *, timeout_s):
        return ExecutorResult(
            id=new_command_id(),
            ok=True,
            result={
                "bridge": {
                    "status": ["SECRET"],
                    "servers": [
                        {
                            "name": "safe",
                            "type": ["invalid"],
                            "enabled": "SECRET",
                        }
                    ],
                },
                "skills": {
                    "sources": [{"source": ["SECRET"], "available": "SECRET"}],
                    "selected": [
                        {"name": "public", "source": {"secret": "SECRET"}}
                    ],
                },
                "health": {"memory_total_bytes": 10**60, "cpu_percent": 9999},
            },
        )

    monkeypatch.setattr(harness.control.executor_transport, "call", invalid)
    with client:
        response = client.get(
            "/api/ui/executors/diagnostics",
            params={"executor_id": harness.executor_id},
        )
        assert response.status_code == 200
        assert "SECRET" not in response.text
        payload = response.json()["data"]["executor"]
        assert payload["bridge"]["status"] == "unavailable"
        assert payload["bridge"]["servers"][0] == {"name": "safe"}
        assert "health" not in payload


def test_diagnostics_require_executor_scope(tmp_path):
    with use_oauth_state(OAuthState(tmp_path / ".oauth-state")):
        client, harness = harness_for(tmp_path, auth="oauth")
        with client, use_role_config(harness.control.config):
            for scope, status in (
                (SCOPE_SHELL_READ, 403),
                (SCOPE_EXECUTOR_USE, 200),
            ):
                token = issue_access_token(
                    client_id="diagnostics-test",
                    scope=scope,
                    resource=f"{BASE}/mcp",
                    subject="owner",
                )
                response = client.get(
                    "/api/ui/executors/diagnostics",
                    params={"executor_id": harness.executor_id},
                    headers={"Authorization": f"Bearer {token}"},
                )
                assert response.status_code == status, response.text
