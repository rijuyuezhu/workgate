from pathlib import Path

import pytest

from workgate.config.settings import Settings
from workgate.executor.agent import (
    activate_agent_skill_execute,
    list_agent_skills_execute,
    read_agent_skill_file_execute,
)
from workgate.executor.runtime import build_executor_runtime


def _runtime_with_project_skill(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg-cache"))
    workspace = tmp_path / "workspace"
    skill_dir = workspace / ".agents" / "skills" / "debugging"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "# Debugging\n\nFind root causes before changing code.\n",
        encoding="utf-8",
    )
    (skill_dir / "guide.md").write_text("Reproduce first.\n", encoding="utf-8")
    settings = Settings(
        workspace_root=workspace,
        state_dir=tmp_path / "state",
        remote_enabled=False,
        agent_bridge_enabled=False,
    )
    runtime = build_executor_runtime(settings, enable_control_connection=False)
    session_id = "sess_0000000000000000000001"
    runtime.services.tool_session_store.create_session(
        session_id=session_id,
        workdir=workspace,
    )
    return settings, runtime, session_id


def test_executor_skill_ops_use_explicit_session_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _settings, runtime, session_id = _runtime_with_project_skill(
        tmp_path, monkeypatch
    )
    config = runtime.config
    store = runtime.services.tool_session_store

    listed = list_agent_skills_execute(config, store, session_id)
    assert [skill["name"] for skill in listed.skills] == ["debugging"]
    assert listed.skills[0]["source"] == "project"

    activated = activate_agent_skill_execute(
        config, store, "debugging", session_id
    )
    assert activated.content.startswith("# Debugging\n")
    assert activated.related_files == ["guide.md"]

    related = read_agent_skill_file_execute(
        config, store, "debugging", "guide.md", session_id
    )
    assert related.content == "Reproduce first.\n"


@pytest.mark.asyncio
async def test_composed_skill_handlers_use_frozen_executor_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, runtime, session_id = _runtime_with_project_skill(
        tmp_path, monkeypatch
    )
    other_workspace = tmp_path / "other-workspace"
    other_workspace.mkdir()

    settings.workspace_root = other_workspace
    settings.max_file_read_bytes = 1
    settings.max_skills = 0

    listed = await runtime.dispatcher.execute(
        "list_agent_skills", {"session_id": session_id}
    )
    assert [skill["name"] for skill in listed.skills] == ["debugging"]

    activated = await runtime.dispatcher.execute(
        "activate_agent_skill",
        {"session_id": session_id, "name": "debugging"},
    )
    assert activated.content.startswith("# Debugging\n")

    related = await runtime.dispatcher.execute(
        "read_agent_skill_file",
        {
            "session_id": session_id,
            "name": "debugging",
            "path": "guide.md",
        },
    )
    assert related.content == "Reproduce first.\n"
