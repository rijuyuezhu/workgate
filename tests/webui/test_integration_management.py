"""Real owner-scoped Integration API CRUD, validation and ACL tests."""

from fastapi.testclient import TestClient

from tests.support.paired import build_paired_http_app
from workgate.config.settings import Settings

BASE = "https://workgate.example"
CONTENT = "---\ndescription: Managed Skill\n---\n# Test\n"


def test_control_and_executor_mcp_and_skill_crud(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    settings = Settings(
        default_workdir=project,
        state_dir=tmp_path / "state",
        auth_mode="none",
        base_url=BASE,
        agent_bridge_enabled=True,
    )
    app, harness = build_paired_http_app(settings)

    def get(client, owner, kind, **extra):
        return client.get(
            "/api/ui/integrations",
            params={
                "owner": owner,
                "kind": kind,
                "executor_id": harness.executor_id,
                **extra,
            },
        )

    def post(client, owner, kind, action, **extra):
        return client.post(
            "/api/ui/integrations",
            json={
                "owner": owner,
                "kind": kind,
                "action": action,
                "executor_id": harness.executor_id,
                **extra,
            },
        )

    with TestClient(app, base_url=BASE) as client:
        assert get(client, "control", "mcp").status_code == 200
        assert get(client, "executor", "mcp").status_code == 200
        assert get(client, "executor", "skill").status_code == 200
        assert (
            post(
                client,
                "control",
                "mcp",
                "register",
                name="remote",
                config={"type": "http", "url": "https://example.org/mcp"},
            ).status_code
            == 200
        )
        control = get(client, "control", "mcp").json()["data"]
        assert control["servers"][0]["name"] == "remote"
        assert (
            post(client, "control", "mcp", "disable", name="remote").status_code
            == 200
        )
        assert (
            get(client, "control", "mcp").json()["data"]["servers"][0][
                "enabled"
            ]
            is False
        )
        assert (
            post(client, "control", "mcp", "remove", name="remote").status_code
            == 200
        )
        assert (
            post(
                client,
                "executor",
                "mcp",
                "register",
                name="stdio",
                config={"type": "stdio", "command": "echo", "args": ["--help"]},
            ).status_code
            == 200
        )
        assert (
            get(client, "executor", "mcp").json()["data"]["servers"][0]["name"]
            == "stdio"
        )
        assert (
            post(client, "executor", "mcp", "disable", name="stdio").status_code
            == 200
        )
        assert (
            post(client, "executor", "mcp", "remove", name="stdio").status_code
            == 200
        )
        created = post(
            client, "executor", "skill", "create", name="note", content=CONTENT
        )
        assert created.status_code == 200, created.text
        revision = created.json()["data"]["skill"]["revision"]
        assert (
            get(client, "executor", "skill").json()["data"]["skills"][0]["name"]
            == "note"
        )
        updated = post(
            client,
            "executor",
            "skill",
            "update",
            name="note",
            content=CONTENT + "More.\n",
            expected_revision=revision,
        )
        assert updated.status_code == 200, updated.text
        next_revision = updated.json()["data"]["skill"]["revision"]
        assert (
            post(
                client,
                "executor",
                "skill",
                "disable",
                name="note",
                expected_revision=next_revision,
            ).status_code
            == 200
        )
        assert (
            post(
                client,
                "executor",
                "skill",
                "delete",
                name="note",
                expected_revision=next_revision,
            ).status_code
            == 409
        )
        latest = get(
            client, "executor", "skill", action="get", name="note"
        ).json()["data"]["skill"]
        assert latest["enabled"] is False
        assert (
            post(
                client,
                "executor",
                "skill",
                "delete",
                name="note",
                expected_revision=latest["revision"],
            ).status_code
            == 200
        )
        assert get(client, "executor", "skill").json()["data"]["skills"] == []
        assert (
            post(
                client,
                "control",
                "skill",
                "create",
                name="bad",
                content=CONTENT,
            ).status_code
            == 400
        )
        assert (
            post(
                client,
                "executor",
                "mcp",
                "register",
                name="bad",
                config={
                    "type": "http",
                    "url": "https://token.example/mcp?secret=value",
                },
            ).status_code
            == 409
        )
        audit = settings.audit_log_path.read_text()
        assert CONTENT not in audit and "https://token.example" not in audit


def test_offline_and_unknown_owner(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    app, harness = build_paired_http_app(
        Settings(
            default_workdir=project,
            state_dir=tmp_path / "state",
            auth_mode="none",
        )
    )

    async def offline(_id):
        return False

    monkeypatch.setattr(
        harness.control.executor_transport, "is_online", offline
    )
    with TestClient(app, base_url=BASE) as client:
        params = {
            "owner": "executor",
            "kind": "skill",
            "executor_id": harness.executor_id,
        }
        assert (
            client.get("/api/ui/integrations", params=params).status_code == 503
        )
        assert (
            client.get(
                "/api/ui/integrations",
                params={"owner": "unknown", "kind": "skill"},
            ).status_code
            == 400
        )
        assert (
            client.post(
                "/api/ui/integrations",
                json={
                    "owner": "executor",
                    "kind": "skill",
                    "action": "create",
                    "name": "x",
                    "content": "hi",
                    "executor_id": harness.executor_id,
                },
            ).status_code
            == 503
        )


def test_oauth_management_requires_separate_write_scope(tmp_path):
    """Listing alone does not grant secret-bearing config read or mutation."""
    from workgate.config.role_config import use_role_config
    from workgate.oauth.core.scopes import (
        SCOPE_EXECUTOR_USE,
        SCOPE_SHELL_READ,
        SCOPE_SHELL_WRITE,
    )
    from workgate.oauth.core.state import OAuthState, use_oauth_state
    from workgate.oauth.protocol.token_codec import issue_access_token

    project = tmp_path / "project"
    project.mkdir()
    settings = Settings(
        default_workdir=project,
        state_dir=tmp_path / "state",
        auth_mode="oauth",
        base_url=BASE,
    )
    with use_oauth_state(OAuthState(tmp_path / ".oauth-state")):
        app, harness = build_paired_http_app(settings)
        with (
            TestClient(app, base_url=BASE) as client,
            use_role_config(harness.control.config),
        ):
            params = {
                "owner": "executor",
                "kind": "skill",
                "executor_id": harness.executor_id,
            }
            for scope, read_status, can_manage in (
                (SCOPE_SHELL_READ, 403, None),
                (SCOPE_EXECUTOR_USE, 200, False),
                (f"{SCOPE_EXECUTOR_USE} {SCOPE_SHELL_WRITE}", 200, True),
            ):
                token = issue_access_token(
                    client_id="integration-test",
                    scope=scope,
                    resource=f"{BASE}/mcp",
                    subject="owner",
                )
                headers = {"Authorization": f"Bearer {token}"}
                response = client.get(
                    "/api/ui/integrations", params=params, headers=headers
                )
                assert response.status_code == read_status, response.text
                if can_manage is not None:
                    assert response.json()["data"]["can_manage"] is can_manage
                if scope == SCOPE_EXECUTOR_USE:
                    assert (
                        client.get(
                            "/api/ui/integrations",
                            params={
                                **params,
                                "action": "get",
                                "name": "missing",
                            },
                            headers=headers,
                        ).status_code
                        == 403
                    )
                    denied = client.post(
                        "/api/ui/integrations",
                        json={
                            **params,
                            "action": "create",
                            "name": "denied",
                            "content": CONTENT,
                        },
                        headers=headers,
                    )
                    assert denied.status_code == 403
                    assert not (
                        harness.executor.config.agent_config_dir
                        / "skills/denied"
                    ).exists()
                if can_manage:
                    created = client.post(
                        "/api/ui/integrations",
                        json={
                            **params,
                            "action": "create",
                            "name": "allowed",
                            "content": CONTENT,
                        },
                        headers=headers,
                    )
                    assert created.status_code == 200, created.text
