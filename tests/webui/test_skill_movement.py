"""Transfer live managed SKILL.md using the existing Owner + Apply interaction."""

from dataclasses import replace
from pathlib import Path

from fastapi.testclient import TestClient

from tests.support.paired import build_paired_http_app
from workgate.config.settings import Settings

BASE = "https://workgate.example"
CONTENT = "---\ndescription: test movement\n---\n# Test\n"


def test_skill_owner_roundtrip_and_safety(tmp_path: Path):
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

    def read(client, owner, name):
        response = client.get(
            "/api/ui/integrations",
            params={
                "owner": owner,
                "executor_id": harness.executor_id,
                "kind": "skill",
                "action": "get",
                "name": name,
            },
        )
        assert response.status_code == 200, response.text
        return response.json()["data"]["skill"]

    def create(client, owner, name, content=CONTENT):
        response = client.post(
            "/api/ui/integrations",
            json={
                "owner": owner,
                "executor_id": harness.executor_id,
                "kind": "skill",
                "action": "create",
                "name": name,
                "content": content,
            },
        )
        assert response.status_code == 200, response.text
        return read(client, owner, name)

    def move(client, source, target, name, revision, content=CONTENT):
        return client.post(
            "/api/ui/integrations/skill-move",
            json={
                "owner": source,
                "executor_id": harness.executor_id,
                "destination_owner": target,
                "destination_executor_id": harness.executor_id,
                "name": name,
                "expected_revision": revision,
                "content": content,
            },
        )

    def missing(client, owner, name):
        response = client.get(
            "/api/ui/integrations",
            params={
                "owner": owner,
                "executor_id": harness.executor_id,
                "kind": "skill",
                "action": "get",
                "name": name,
            },
        )
        assert response.status_code == 409, response.text

    with TestClient(app, base_url=BASE) as client:
        first = create(client, "control", "transfer")
        edited = CONTENT + "Updated in Apply.\n"
        moved = move(
            client, "control", "executor", "transfer", first["revision"], edited
        )
        assert moved.status_code == 200, moved.text
        missing(client, "control", "transfer")
        result = read(client, "executor", "transfer")
        assert result["content"] == edited and result["enabled"]
        assert not result["has_assets"]

        back = move(
            client, "executor", "control", "transfer", result["revision"]
        )
        assert back.status_code == 200, back.text
        missing(client, "executor", "transfer")
        assert read(client, "control", "transfer")["enabled"]

        stale = move(client, "control", "executor", "transfer", "a" * 64)
        assert stale.status_code == 409
        missing(client, "executor", "transfer")

        existing = create(client, "executor", "transfer")
        collision = move(
            client,
            "control",
            "executor",
            "transfer",
            read(client, "control", "transfer")["revision"],
        )
        assert collision.status_code == 409
        assert read(client, "executor", "transfer") == existing
        assert read(client, "control", "transfer")["enabled"]

        other = create(client, "control", "disabled")
        response = client.post(
            "/api/ui/integrations",
            json={
                "owner": "control",
                "kind": "skill",
                "action": "disable",
                "name": "disabled",
                "expected_revision": other["revision"],
            },
        )
        assert response.status_code == 200
        current = read(client, "control", "disabled")
        assert not current["enabled"]
        moved = move(
            client, "control", "executor", "disabled", current["revision"]
        )
        assert moved.status_code == 200, moved.text
        assert read(client, "executor", "disabled")["enabled"] is False
        missing(client, "control", "disabled")

        asset_skill = create(client, "control", "bundle")
        bundle = tmp_path / "control-agent" / "skills" / "bundle" / "examples"
        bundle.mkdir()
        (bundle / "notes.md").write_text("Important auxiliary file")
        denied = move(
            client, "control", "executor", "bundle", asset_skill["revision"]
        )
        assert denied.status_code == 409
        assert (bundle / "notes.md").read_text() == "Important auxiliary file"
        missing(client, "executor", "bundle")


def test_skill_move_race_rollback_is_revision_guarded(tmp_path, monkeypatch):
    """Do not remove the only copy or delete a newer destination edit."""
    from workgate.agent_bridge.skill_management import manage_skill

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
    source_dir = tmp_path / "control-agent"
    target_dir = tmp_path / "executor-agent"
    harness.control.config = replace(
        harness.control.config, agent_config_dir=source_dir
    )
    harness.executor.config = replace(
        harness.executor.config, agent_config_dir=target_dir
    )
    original_call = harness.control.executor_transport.call
    race_mode = "source"
    updates = 0

    async def competing_call(executor_id, op, args, **kwargs):
        nonlocal updates
        response = await original_call(executor_id, op, args, **kwargs)
        if (
            op == "ui.skill.manage"
            and args["action"] == "stage"
            and response.ok
        ):
            source = manage_skill(source_dir, "get", name="race")["skill"]
            updates += 1
            manage_skill(
                source_dir,
                "update",
                name="race",
                content=CONTENT + f" source edit {updates}\n",
                expected_revision=source["revision"],
            )
            if race_mode == "both":
                assert isinstance(response.result, dict)
                staged = response.result["skill"]
                assert isinstance(staged, dict)
                await original_call(
                    executor_id,
                    op,
                    {
                        "action": "update",
                        "name": "race",
                        "content": CONTENT + " destination edit\n",
                        "expected_revision": staged["revision"],
                    },
                    **kwargs,
                )
        return response

    monkeypatch.setattr(
        harness.control.executor_transport, "call", competing_call
    )
    with TestClient(app, base_url=BASE) as client:

        def api(owner, action, **kwargs):
            return client.post(
                "/api/ui/integrations",
                json={
                    "owner": owner,
                    "executor_id": harness.executor_id,
                    "kind": "skill",
                    "action": action,
                    "name": "race",
                    **kwargs,
                },
            )

        def get(owner):
            response = client.get(
                "/api/ui/integrations",
                params={
                    "owner": owner,
                    "executor_id": harness.executor_id,
                    "kind": "skill",
                    "action": "get",
                    "name": "race",
                },
            )
            assert response.status_code == 200, response.text
            return response.json()["data"]["skill"]

        def move():
            return client.post(
                "/api/ui/integrations/skill-move",
                json={
                    "owner": "control",
                    "destination_owner": "executor",
                    "destination_executor_id": harness.executor_id,
                    "name": "race",
                    "expected_revision": get("control")["revision"],
                    "content": CONTENT,
                },
            )

        assert api("control", "create", content=CONTENT).status_code == 200
        response = move()
        assert response.status_code == 409, response.text
        assert get("control")["content"].endswith("source edit 1\n")
        missing = client.get(
            "/api/ui/integrations",
            params={
                "owner": "executor",
                "executor_id": harness.executor_id,
                "kind": "skill",
                "action": "get",
                "name": "race",
            },
        )
        assert missing.status_code == 409  # staged target rolled back

        race_mode = "both"
        response = move()
        assert (
            response.status_code == 503
        )  # manual recovery, no unsafe rollback
        assert get("control")["content"].endswith("source edit 2\n")
        destination = get("executor")
        assert destination["content"].endswith("destination edit\n")
        assert destination["enabled"] is False
