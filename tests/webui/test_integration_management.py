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

    def change(client, owner, kind, action, name):
        fetched = get(client, owner, kind, action="get", name=name)
        assert fetched.status_code == 200, fetched.text
        revision = fetched.json()["data"][
            "server" if kind == "mcp" else "skill"
        ]["revision"]
        return post(
            client, owner, kind, action, name=name, expected_revision=revision
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
            == 400
        )
        assert (
            change(
                client, "control", "mcp", "disable", name="remote"
            ).status_code
            == 200
        )
        assert (
            get(client, "control", "mcp").json()["data"]["servers"][0][
                "enabled"
            ]
            is False
        )
        assert (
            change(
                client, "control", "mcp", "remove", name="remote"
            ).status_code
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
            change(
                client, "executor", "mcp", "disable", name="stdio"
            ).status_code
            == 200
        )
        assert (
            change(
                client, "executor", "mcp", "remove", name="stdio"
            ).status_code
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


def test_mcp_auth_management_private_store_and_owner_authority(tmp_path):
    """Control HTTP and remote stdio credentials never enter responses or Audit."""
    from workgate.agent_bridge.auth_store import AgentAuthStore

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
    secret = "EXTREMELY_PRIVATE_AUTH_TOKEN_69405"

    def post(client, *, owner, name, action, **kwargs):
        return client.post(
            "/api/ui/integrations/auth",
            json={
                "owner": owner,
                "name": name,
                "action": action,
                "executor_id": harness.executor_id,
                **kwargs,
            },
        )

    def status(client, owner, name):
        return client.get(
            "/api/ui/integrations/auth",
            params={
                "owner": owner,
                "name": name,
                "executor_id": harness.executor_id,
            },
        )

    with TestClient(app, base_url=BASE) as client:
        for owner, name, config in (
            (
                "control",
                "remote",
                {"type": "http", "url": "https://example.org/mcp"},
            ),
            ("executor", "local", {"type": "stdio", "command": "echo"}),
        ):
            added = client.post(
                "/api/ui/integrations",
                json={
                    "owner": owner,
                    "kind": "mcp",
                    "action": "register",
                    "executor_id": harness.executor_id,
                    "name": name,
                    "config": config,
                },
            )
            assert added.status_code == 200, added.text
            initial = status(client, owner, name)
            assert initial.status_code == 200, initial.text
            assert initial.json()["data"]["auth"]["status"] == "not_required"
            changed = post(
                client,
                owner=owner,
                name=name,
                action="set_secret",
                secret_name="api_key",
                secret_value=secret,
            )
            assert changed.status_code == 200, changed.text
            assert secret not in changed.text
            assert changed.json()["data"]["secret_names"] == ["api_key"]
            assert status(client, owner, name).json()["data"][
                "secret_names"
            ] == ["api_key"]
            removed = post(
                client,
                owner=owner,
                name=name,
                action="delete_secret",
                secret_name="api_key",
            )
            assert removed.status_code == 200
            assert removed.json()["data"]["secret_names"] == []
            assert secret not in removed.text
        assert (
            post(
                client,
                owner="executor",
                name="remote",
                action="set_secret",
                secret_name="api_key",
                secret_value=secret,
            ).status_code
            == 409
        )
        assert (
            post(
                client,
                owner="control",
                name="local",
                action="set_secret",
                secret_name="api_key",
                secret_value=secret,
            ).status_code
            == 409
        )
        assert (
            post(
                client,
                owner="control",
                name="missing",
                action="set_secret",
                secret_name="api_key",
                secret_value=secret,
            ).status_code
            == 409
        )
        assert (
            post(
                client, owner="control", name="remote", action="clear_oauth"
            ).status_code
            == 409
        )
        invalid = post(
            client,
            owner="control",
            name="remote",
            action="set_secret",
            secret_name="api_key",
            secret_value="",
        )
        assert invalid.status_code == 409
        assert secret not in settings.audit_log_path.read_text()
        assert not AgentAuthStore(settings.agent_auth_dir).list_secrets()


def test_mcp_auth_endpoint_denied_without_management_scope(tmp_path):
    from workgate.config.role_config import use_role_config
    from workgate.oauth.core.scopes import SCOPE_EXECUTOR_USE, SCOPE_SHELL_WRITE
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
            for scope, expected in (
                (SCOPE_EXECUTOR_USE, 403),
                (SCOPE_SHELL_WRITE, 403),
                (f"{SCOPE_EXECUTOR_USE} {SCOPE_SHELL_WRITE}", 409),
            ):
                token = issue_access_token(
                    client_id="auth-test",
                    scope=scope,
                    resource=f"{BASE}/mcp",
                    subject="owner",
                )
                headers = {"Authorization": f"Bearer {token}"}
                response = client.get(
                    "/api/ui/integrations/auth",
                    params={
                        "owner": "control",
                        "name": "unknown",
                    },
                    headers=headers,
                )
                assert response.status_code == expected, response.text
                mutation = client.post(
                    "/api/ui/integrations/auth",
                    json={
                        "owner": "control",
                        "name": "unknown",
                        "action": "set_secret",
                        "secret_name": "key",
                        "secret_value": "CREDENTIAL_VALUE",
                    },
                    headers=headers,
                )
                assert mutation.status_code == expected, mutation.text
                assert "CREDENTIAL_VALUE" not in mutation.text


def test_authenticated_mcp_registration_assigns_stable_identity(tmp_path):
    """Users may select OAuth/Secret Auth without inventing a store identity."""
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
    with TestClient(app, base_url=BASE) as client:
        cases = (
            (
                "control",
                "remote-oauth",
                {
                    "type": "http",
                    "url": "https://example.org/mcp",
                    "auth": {"mode": "oauth", "scopes": ["read"]},
                },
            ),
            (
                "executor",
                "local-secret",
                {
                    "type": "stdio",
                    "command": "echo",
                    "env": {"API_TOKEN": {"secret": "api_token"}},
                    "auth": {"mode": "secret"},
                },
            ),
        )
        for owner, name, config in cases:
            response = client.post(
                "/api/ui/integrations",
                json={
                    "owner": owner,
                    "kind": "mcp",
                    "action": "register",
                    "executor_id": harness.executor_id,
                    "name": name,
                    "config": config,
                },
            )
            assert response.status_code == 200, response.text
            listed = client.get(
                "/api/ui/integrations",
                params={
                    "owner": owner,
                    "kind": "mcp",
                    "executor_id": harness.executor_id,
                },
            )
            assert listed.status_code == 200, listed.text
            server = next(
                row
                for row in listed.json()["data"]["servers"]
                if row["name"] == name
            )
            identity = server["integrationId"]
            assert isinstance(identity, str) and identity.startswith("ui-")
            assert len(identity) <= 128
            detail = client.get(
                "/api/ui/integrations",
                params={
                    "owner": owner,
                    "kind": "mcp",
                    "executor_id": harness.executor_id,
                    "action": "get",
                    "name": name,
                },
            )
            assert detail.status_code == 200, detail.text
            assert (
                detail.json()["data"]["editable_config"]["integrationId"]
                == identity
            )
            assert (
                client.post(
                    "/api/ui/integrations",
                    json={
                        "owner": owner,
                        "kind": "mcp",
                        "action": "remove",
                        "executor_id": harness.executor_id,
                        "name": name,
                        "expected_revision": server["revision"],
                    },
                ).status_code
                == 200
            )
