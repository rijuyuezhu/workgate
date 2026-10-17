"""Real Control/Executor Skill source precedence without cross-host file copies."""

from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest

from workgate.config.control import resolve_control_config
from workgate.config.executor import resolve_executor_config
from workgate.config.settings import Settings
from workgate.control.agent_bridge import ControlAgentBridgeService
from workgate.control.sessions import ControlSessionCoordinator
from workgate.executor.runtime import build_executor_runtime

SESSION = "sess_0000000000000000000001"


def install(root: Path, name: str, source: str) -> None:
    skill = root / name
    skill.mkdir(parents=True, exist_ok=True)
    (skill / "SKILL.md").write_text(
        f"---\ndescription: {source}\n---\n# {source}\n", encoding="utf-8"
    )
    (skill / "guide.md").write_text(f"Guide from {source}\n", encoding="utf-8")


class LocalSessions:
    """Run actual Executor handlers using a frozen session-bound workdir."""

    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime
        self.offline = False

    async def call_session_tool(self, name: str, args: dict[str, Any]) -> Any:
        if self.offline:
            raise RuntimeError("Executor offline")
        result = await self.runtime.dispatcher.execute(name, args)
        return result.model_dump(mode="json")


def setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    project = tmp_path / "project"
    project.mkdir()
    control_dir = tmp_path / "control-agent"
    executor_dir = tmp_path / "executor-agent"
    control = Settings(
        default_workdir=project,
        state_dir=tmp_path / "control-state",
        auth_mode="none",
    )
    executor = control.model_copy(
        update={
            "state_dir": tmp_path / "executor-state",
            "agent_config_dir": executor_dir,
        }
    )
    runtime = build_executor_runtime(
        replace(
            resolve_executor_config(executor), agent_config_dir=executor_dir
        ),
        enable_control_connection=False,
    )
    runtime.services.tool_session_store.create_session(
        session_id=SESSION, workdir=project
    )
    sessions = LocalSessions(runtime)
    bridge = ControlAgentBridgeService(
        replace(resolve_control_config(control), agent_config_dir=control_dir),
        cast(ControlSessionCoordinator, sessions),
    )
    return (
        bridge,
        sessions,
        project,
        executor_dir,
        control_dir,
        tmp_path / "xdg",
    )


@pytest.mark.asyncio
async def test_all_four_skill_tiers_with_matching_activation_and_file_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge, _sessions, project, executor, control, global_dir = setup(
        tmp_path, monkeypatch
    )
    install(control / "skills", "shared", "control")
    install(control / "skills", "control-only", "control")
    install(executor / "skills", "shared", "managed")
    install(executor / "skills", "managed-only", "managed")
    install(global_dir / "agents" / "skills", "shared", "global")
    install(global_dir / "agents" / "skills", "global-only", "global")
    install(project / ".agents" / "skills", "shared", "project")

    listed = await bridge.list_skills(SESSION)
    effective = {row["name"]: row["source"] for row in listed.skills}
    assert effective == {
        "shared": "project",
        "managed-only": "managed",
        "global-only": "global",
        "control-only": "control",
    }
    assert [row["source"] for row in listed.sources] == [
        "project",
        "managed",
        "global",
        "control",
    ]
    assert any(
        "control: skipped duplicate Skill 'shared'" in w
        for w in listed.warnings
    )

    # Without a Session only Control-owned resources are selected, even for a
    # name shadowed by a higher-priority Project Skill on a specific Executor.
    assert (await bridge.activate_skill(None, "shared")).source == "control"
    assert {row["name"] for row in (await bridge.list_skills(None)).skills} == {
        "shared",
        "control-only",
    }

    for name, source in effective.items():
        active = await bridge.activate_skill(SESSION, name)
        assert active.source == source
        assert f"# {source}" in active.content
        assert active.related_files == ["guide.md"]
        related = await bridge.read_skill_file(SESSION, name, "guide.md")
        assert related.source == source
        assert related.content == f"Guide from {source}\n"


@pytest.mark.asyncio
async def test_disabled_and_invalid_control_sources_stay_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge, sessions, _project, _executor, control, _xdg = setup(
        tmp_path, monkeypatch
    )
    install(control / "skills", "control-only", "control")
    (control / "config.json").write_text(
        '{"version":1,"skills":{"enabled":true,"disabled":["control-only"]}}'
    )
    assert not (await bridge.list_skills(SESSION)).skills
    with pytest.raises(ValueError, match="Unknown agent skill"):
        await bridge.activate_skill(SESSION, "control-only")
    (control / "config.json").write_text(
        '{"version":1,"skills":{"enabled":true,"disabled":[]}}'
    )
    assert (
        await bridge.activate_skill(SESSION, "control-only")
    ).source == "control"
    (control / "config.json").write_text("{broken json")
    listed = await bridge.list_skills(SESSION)
    assert not listed.skills
    assert "Control Skill manifest invalid" in listed.warnings
    sessions.offline = True
    with pytest.raises(RuntimeError, match="Executor offline"):
        await bridge.list_skills(SESSION)
    with pytest.raises(RuntimeError, match="Executor offline"):
        await bridge.activate_skill(SESSION, "control-only")
    # Explicitly omitting Session stays usable without an online Executor,
    # never claiming project or Executor source precedence while offline.
    (control / "config.json").write_text(
        '{"version":1,"skills":{"enabled":true,"disabled":[]}}'
    )
    control_only = await bridge.list_skills(None)
    assert [(row["name"], row["source"]) for row in control_only.skills] == [
        ("control-only", "control")
    ]
    assert (
        await bridge.activate_skill(None, "control-only")
    ).source == "control"
    assert (
        await bridge.read_skill_file(None, "control-only", "guide.md")
    ).content == "Guide from control\n"


@pytest.mark.asyncio
async def test_control_skills_visible_to_multiple_session_executors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge, _sessions, _project, _executor, control, _xdg = setup(
        tmp_path, monkeypatch
    )
    install(control / "skills", "control-only", "control")
    settings2 = Settings(
        default_workdir=tmp_path / "project2",
        state_dir=tmp_path / "executor2-state",
    )
    (tmp_path / "project2").mkdir()
    runtime2 = build_executor_runtime(
        replace(
            resolve_executor_config(settings2),
            agent_config_dir=tmp_path / "executor2-agent",
        ),
        enable_control_connection=False,
    )
    runtime2.services.tool_session_store.create_session(
        session_id=SESSION, workdir=tmp_path / "project2"
    )
    bridge2 = ControlAgentBridgeService(
        bridge._settings,
        cast(ControlSessionCoordinator, LocalSessions(runtime2)),
    )
    assert (await bridge.list_skills(SESSION)).skills[-1][
        "name"
    ] == "control-only"
    assert (await bridge2.list_skills(SESSION)).skills[-1][
        "name"
    ] == "control-only"
    assert (
        await bridge2.activate_skill(SESSION, "control-only")
    ).source == "control"


def test_public_control_skill_tools_across_real_paired_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """HTTP/MCP ToolRouter → real session → Executor, then Control fallback."""
    from fastapi.testclient import TestClient

    from tests.support.paired import build_paired_http_app

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-home"))
    project = tmp_path / "project"
    project.mkdir()
    settings = Settings(
        default_workdir=project,
        state_dir=tmp_path / "state",
        auth_mode="none",
        agent_bridge_enabled=True,
        base_url="https://workgate.example",
    )
    app, harness = build_paired_http_app(settings)
    install(settings.agent_config_dir / "skills", "tiered", "control")
    install(settings.agent_config_dir / "skills", "control-fallback", "control")
    install(
        harness.executor.config.agent_config_dir / "skills",
        "tiered",
        "executor",
    )
    install(project / ".agents" / "skills", "tiered", "project")

    with TestClient(app, base_url="https://workgate.example") as client:

        def invoke(tool: str, data: dict):
            response = client.post(f"/tools/{tool}", json=data)
            assert response.status_code == 200, response.text
            payload = response.json()
            assert payload.get("isError") is not True, payload
            return payload["structuredContent"]

        standalone = invoke("list_agent_skills", {})
        assert {row["name"] for row in standalone["skills"]} == {
            "tiered",
            "control-fallback",
        }
        assert {row["source"] for row in standalone["skills"]} == {"control"}
        sid = invoke(
            "session_start",
            {"executor_id": harness.executor_id, "workdir": str(project)},
        )["session_id"]
        effective = invoke("list_agent_skills", {"session_id": sid})
        assert {row["name"]: row["source"] for row in effective["skills"]} == {
            "tiered": "project",
            "control-fallback": "control",
        }
        selected = invoke(
            "activate_agent_skill", {"name": "tiered", "session_id": sid}
        )
        assert selected["source"] == "project"
        assert "# project" in selected["content"]
        fallback = invoke(
            "activate_agent_skill",
            {"name": "control-fallback", "session_id": sid},
        )
        assert fallback["source"] == "control"
        related = invoke(
            "read_agent_skill_file",
            {"name": "control-fallback", "path": "guide.md", "session_id": sid},
        )
        assert related["source"] == "control"
        assert related["content"] == "Guide from control\n"
        no_session = invoke("activate_agent_skill", {"name": "tiered"})
        assert no_session["source"] == "control"
        assert "# control" in no_session["content"]
        # A provided but invalid Session must not silently drop to Control.
        denied = client.post(
            "/tools/activate_agent_skill",
            json={"name": "control-fallback", "session_id": "sess_invalid"},
        )
        assert denied.status_code >= 400 or denied.json().get("isError") is True
        outside = client.post(
            "/tools/read_agent_skill_file",
            json={"name": "control-fallback", "path": "../outside.md"},
        )
        assert (
            outside.status_code >= 400 or outside.json().get("isError") is True
        )
