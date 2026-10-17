"""HTTP MCP entrypoint routes managed Skill writes to selected Executor."""

import json

import pytest
from fastapi.testclient import TestClient

from tests.support.paired import build_paired_http_app
from workgate.config.settings import Settings

BASE = "https://workgate.example"
ENTRY = (
    "---\ndescription: Skill metadata\n---\n# Managed skill\nSafe content.\n"
)


def test_model_tool_routes_to_executor_and_redacts_audit(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    settings = Settings(
        default_workdir=project,
        state_dir=tmp_path / "state",
        agent_bridge_enabled=True,
        auth_mode="none",
        base_url=BASE,
    )
    app, harness = build_paired_http_app(settings)
    with TestClient(app, base_url=BASE) as client:
        started = client.post(
            "/tools/session_start",
            json={
                "executor_id": harness.executor_id,
                "workdir": str(project),
            },
        ).json()["structuredContent"]
        sid = started["session_id"]
        request = {
            "session_id": sid,
            "action": "create",
            "name": "notes",
            "content": ENTRY,
        }
        response = client.post("/tools/manage_agent_skill", json=request)
        assert response.status_code == 200, response.text
        created = response.json()["structuredContent"]
        assert created["skill"]["name"] == "notes"
        assert created["skill"]["content"] == ENTRY
        revision = created["skill"]["revision"]
        assert (
            harness.executor.config.agent_config_dir / "skills/notes/SKILL.md"
        ).read_text() == ENTRY
        listed = client.post(
            "/tools/manage_agent_skill",
            json={
                "session_id": sid,
                "action": "list",
            },
        ).json()["structuredContent"]
        assert [item["name"] for item in listed["skills"]] == ["notes"]
        disabled = client.post(
            "/tools/manage_agent_skill",
            json={
                "session_id": sid,
                "action": "disable",
                "name": "notes",
                "expected_revision": revision,
            },
        ).json()["structuredContent"]
        assert disabled["skill"]["configured_enabled"] is False
        denied = client.post(
            "/tools/manage_agent_skill",
            json={
                "session_id": "sess_not_owned",
                "action": "list",
            },
        )
        assert (
            denied.status_code in {400, 404}
            or denied.json().get("isError") is True
        )
        audit_text = settings.audit_log_path.read_text()
        assert ENTRY not in audit_text
        assert "content" not in json.dumps(
            [
                event.get("input")
                for event in map(json.loads, audit_text.splitlines())
                if event.get("tool") == "manage_agent_skill"
            ]
        )
        assert '"redacted":true' in audit_text.replace(" ", "")


@pytest.mark.asyncio
async def test_executor_management_rpc_does_not_require_an_active_session(
    tmp_path,
):
    from tests.support.paired import build_paired_control_harness
    from workgate.protocol.ids import new_session_id

    project = tmp_path / "project"
    project.mkdir()
    settings = Settings(
        default_workdir=project,
        state_dir=tmp_path / "state",
        agent_bridge_enabled=True,
        auth_mode="none",
    )
    harness = build_paired_control_harness(settings)
    result = await harness.call(
        harness.executor_id,
        "ui.skill.manage",
        {
            "action": "create",
            "name": "admin",
            "content": ENTRY,
        },
    )
    assert result.ok and isinstance(result.result, dict)
    created_skill = result.result.get("skill")
    assert (
        isinstance(created_skill, dict) and created_skill.get("name") == "admin"
    )
    listed = await harness.call(
        harness.executor_id, "ui.skill.manage", {"action": "list"}
    )
    assert listed.ok and isinstance(listed.result, dict)
    skills = listed.result.get("skills")
    assert isinstance(skills, list) and len(skills) == 1
    assert isinstance(skills[0], dict) and skills[0].get("name") == "admin"
    with_session = await harness.call(
        harness.executor_id,
        "ui.skill.manage",
        {"action": "list"},
        session_id=str(new_session_id()),
    )
    assert not with_session.ok
