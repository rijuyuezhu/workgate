"""Combinatorial owner-scoped CRUD against real paired Control / Executor APIs."""

from dataclasses import replace

from fastapi.testclient import TestClient

from tests.support.paired import build_paired_http_app
from workgate.config.settings import Settings

BASE = "https://workgate.example"


def test_owner_kind_enabled_edit_conflict_matrix(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    app, harness = build_paired_http_app(
        Settings(
            default_workdir=project,
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

    def api(client, owner, kind, action, name, **values):
        return client.post(
            "/api/ui/integrations",
            json={
                "owner": owner,
                "kind": kind,
                "executor_id": harness.executor_id,
                "action": action,
                "name": name,
                **values,
            },
        )

    def get(client, owner, kind, name):
        response = client.get(
            "/api/ui/integrations",
            params={
                "owner": owner,
                "kind": kind,
                "executor_id": harness.executor_id,
                "action": "get",
                "name": name,
            },
        )
        assert response.status_code == 200, response.text
        return response.json()["data"]

    with TestClient(app, base_url=BASE) as client:
        for kind in ("mcp", "skill"):
            # Same name at both owners must remain independently configurable.
            for owner in ("control", "executor"):
                name = f"matrix-{kind}"
                initial = (
                    {"content": "---\ndescription: Matrix\n---\n# Initial\n"}
                    if kind == "skill"
                    else {
                        "config": (
                            {"type": "http", "url": "https://example.org/mcp"}
                            if owner == "control"
                            else {"type": "stdio", "command": "echo"}
                        )
                    }
                )
                create_action = "create" if kind == "skill" else "register"
                created = api(
                    client, owner, kind, create_action, name, **initial
                )
                assert created.status_code == 200, (owner, kind, created.text)
                assert (
                    api(
                        client, owner, kind, create_action, name, **initial
                    ).status_code
                    == 409
                )
                row_name = "skill" if kind == "skill" else "server"
                original = get(client, owner, kind, name)[row_name]
                assert original["enabled"]
                assert (
                    api(
                        client,
                        owner,
                        kind,
                        "disable",
                        name,
                        expected_revision="0" * 64,
                    ).status_code
                    == 409
                )
                disabled = api(
                    client,
                    owner,
                    kind,
                    "disable",
                    name,
                    expected_revision=original["revision"],
                )
                assert disabled.status_code == 200, disabled.text
                current = get(client, owner, kind, name)[row_name]
                assert current["enabled"] is False

                update = (
                    {"content": "---\ndescription: Matrix\n---\n# Edited\n"}
                    if kind == "skill"
                    else {
                        "config": {
                            **get(client, owner, kind, name)["editable_config"],
                            **(
                                {"url": "https://example.org/updated"}
                                if owner == "control"
                                else {"command": "printf"}
                            ),
                            "enabled": False,
                        }
                    }
                )
                revised = api(
                    client,
                    owner,
                    kind,
                    "update",
                    name,
                    expected_revision=current["revision"],
                    **update,
                )
                assert revised.status_code == 200, revised.text
                edited = get(client, owner, kind, name)[row_name]
                assert edited["enabled"] is False
                assert edited["revision"] != current["revision"]
                assert (
                    api(
                        client,
                        owner,
                        kind,
                        "enable",
                        name,
                        expected_revision=current["revision"],
                    ).status_code
                    == 409
                )
                enabled = api(
                    client,
                    owner,
                    kind,
                    "enable",
                    name,
                    expected_revision=edited["revision"],
                )
                assert enabled.status_code == 200, enabled.text
                latest = get(client, owner, kind, name)[row_name]
                assert latest["enabled"] is True
                assert (
                    api(
                        client,
                        owner,
                        kind,
                        "remove" if kind == "mcp" else "delete",
                        name,
                        expected_revision=edited["revision"],
                    ).status_code
                    == 409
                )

            # The same name is listed under both authorities, independently.
            for owner in ("control", "executor"):
                item = get(client, owner, kind, name)[row_name]
                if kind == "skill":
                    assert "# Edited" in item["content"]
                else:
                    configuration = get(client, owner, kind, name)[
                        "editable_config"
                    ]
                    assert configuration[
                        "url" if owner == "control" else "command"
                    ] == (
                        "https://example.org/updated"
                        if owner == "control"
                        else "printf"
                    )
                deleted = api(
                    client,
                    owner,
                    kind,
                    "delete" if kind == "skill" else "remove",
                    name,
                    expected_revision=item["revision"],
                )
                assert deleted.status_code == 200, deleted.text
                assert (
                    client.get(
                        "/api/ui/integrations",
                        params={
                            "owner": owner,
                            "kind": kind,
                            "executor_id": harness.executor_id,
                            "action": "get",
                            "name": name,
                        },
                    ).status_code
                    == 409
                )


def test_invalid_ownership_transport_and_revision_matrix(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    app, harness = build_paired_http_app(
        Settings(
            default_workdir=project,
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
    with TestClient(app, base_url=BASE) as client:
        for owner, config in (
            ("control", {"type": "stdio", "command": "echo"}),
            ("executor", {"type": "http", "url": "https://example.org/mcp"}),
            (
                "control",
                {"type": "http", "url": "https://user:pw@example.org/mcp"},
            ),
            ("executor", {"type": "stdio", "command": ""}),
        ):
            result = client.post(
                "/api/ui/integrations",
                json={
                    "owner": owner,
                    "kind": "mcp",
                    "action": "register",
                    "executor_id": harness.executor_id,
                    "name": "invalid-mcp",
                    "config": config,
                },
            )
            assert result.status_code == 409, result.text
        for owner in ("control", "executor"):
            for content in ("", "---\ndescription: [\n---\n# Bad\n", " \n"):
                result = client.post(
                    "/api/ui/integrations",
                    json={
                        "owner": owner,
                        "kind": "skill",
                        "action": "create",
                        "executor_id": harness.executor_id,
                        "name": "invalid-skill",
                        "content": content,
                    },
                )
                assert result.status_code in {400, 409}, result.text
        for owner in ("control", "executor"):
            result = client.post(
                "/api/ui/integrations",
                json={
                    "owner": owner,
                    "kind": "mcp",
                    "action": "remove",
                    "executor_id": harness.executor_id,
                    "name": "does-not-exist",
                },
            )
            assert result.status_code == 400
