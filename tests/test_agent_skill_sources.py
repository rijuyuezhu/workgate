from pathlib import Path
from typing import Any

import pytest

import workgate.agent_bridge.sources as source_module
from workgate.agent_bridge.models import SkillSource as ModelSkillSource
from workgate.agent_bridge.registry import build_agent_registry
from workgate.agent_bridge.service import (
    activate_agent_skill_payload,
    list_agent_skills_payload,
    read_agent_skill_file_payload,
    tool_value,
)
from workgate.agent_bridge.sources import (
    SkillSource,
    scan_skill_sources,
    skill_sources,
)
from workgate.agent_bridge.state import (
    agent_config_fingerprint,
    agent_registry_fingerprint,
)
from workgate.agent_bridge.tools import AgentBridgeToolReloader
from workgate.config.settings import clear_settings_cache, get_settings
from workgate.executor.runtime import build_executor_runtime
from workgate.executor.tool_session.store import get_tool_session_store


class _NoopClientManager:
    async def list_tools(self, name: str, config: Any) -> list[Any]:  # noqa: ARG002
        return []


def _install_skill(root: Path, directory: str, name: str, marker: str) -> Path:
    skill = root / directory / name
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        f"# {name}\n\n{marker}.\n", encoding="utf-8"
    )
    (skill / "guide.md").write_text(marker, encoding="utf-8")
    return skill


def _configure(
    monkeypatch: pytest.MonkeyPatch,
    *,
    workspace: Path,
    state_dir: Path,
    config_dir: Path,
    xdg_config_home: Path,
) -> None:
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(workspace))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(state_dir))
    monkeypatch.setenv("WORKGATE_AGENT_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("WORKGATE_AUTH_MODE", "none")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg_config_home))
    clear_settings_cache()
    get_tool_session_store().clear()


def test_sources_reexport_the_model_skill_source_contract() -> None:
    assert SkillSource is ModelSkillSource


def test_skill_sources_deduplicate_equivalent_roots(tmp_path: Path) -> None:
    root = tmp_path / "shared"
    xdg = tmp_path / "xdg"

    sources = skill_sources(
        project_root=root,
        managed_config_dir=root,
        managed_directory=".agents/skills",
        environ={"XDG_CONFIG_HOME": str(xdg)},
    )

    assert [source.name for source in sources] == ["project", "global"]


def test_source_warning_limit_adds_one_omission_marker() -> None:
    warnings = [
        f"warning-{index}" for index in range(source_module.MAX_SOURCE_WARNINGS)
    ]

    source_module._append_warning(warnings, "first omitted warning")
    source_module._append_warning(warnings, "second omitted warning")

    assert warnings[-1] == "Additional Skill source warnings were omitted"
    assert len(warnings) == source_module.MAX_SOURCE_WARNINGS + 1


def test_agent_config_fingerprint_accepts_regular_file_root(
    tmp_path: Path,
) -> None:
    root = tmp_path / "config.json"
    root.write_text("{}", encoding="utf-8")

    first = agent_config_fingerprint(root)
    second = agent_config_fingerprint(root)

    assert first == second
    assert len(first) == 64


def test_skill_sources_use_project_managed_global_order_and_ignore_relative_xdg(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    project = tmp_path / "project"
    managed = tmp_path / "managed"
    project.mkdir()
    managed.mkdir()
    absolute_xdg = tmp_path / "xdg"

    sources = skill_sources(
        project_root=project,
        managed_config_dir=managed,
        managed_directory="skills",
        environ={"XDG_CONFIG_HOME": str(absolute_xdg)},
    )
    fallback = skill_sources(
        project_root=project,
        managed_config_dir=managed,
        managed_directory="skills",
        environ={"XDG_CONFIG_HOME": "relative/config"},
    )

    assert [source.name for source in sources] == [
        "project",
        "managed",
        "global",
    ]
    assert sources[0].path == (project / ".agents/skills").resolve()
    assert sources[1].path == (managed / "skills").resolve()
    assert sources[2].path == (absolute_xdg / "agents/skills").resolve()
    assert fallback[2].path == (Path.home() / ".config/agents/skills").resolve()


def test_registry_prioritizes_project_then_managed_then_global(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    project = tmp_path / "project"
    managed = tmp_path / "managed"
    xdg = tmp_path / "xdg"
    project.mkdir()
    managed.mkdir()
    _install_skill(project, ".agents/skills", "duplicate", "project copy")
    _install_skill(managed, "skills", "duplicate", "managed copy")
    _install_skill(managed, "skills", "managed-only", "managed only")
    _install_skill(xdg, "agents/skills", "duplicate", "global copy")
    _install_skill(xdg, "agents/skills", "global-only", "global only")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))

    registry = build_agent_registry(
        managed,
        _NoopClientManager(),
        project_root=project,
        dynamic_mcp_tools=False,
        dynamic_skill_tools=False,
    )

    assert list(registry.skills) == ["duplicate", "managed-only", "global-only"]
    assert registry.skills["duplicate"].source == "project"
    assert registry.skills["managed-only"].source == "managed"
    assert registry.skills["global-only"].source == "global"
    assert (
        sum(
            "duplicate Skill 'duplicate'" in warning
            for warning in registry.skill_warnings
        )
        == 2
    )


def test_invalid_project_duplicate_falls_back_to_managed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    project = tmp_path / "project"
    managed = tmp_path / "managed"
    xdg = tmp_path / "xdg"
    (project / ".agents/skills/fallback").mkdir(parents=True)
    managed.mkdir()
    _install_skill(managed, "skills", "fallback", "managed valid")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))

    registry = build_agent_registry(
        managed,
        _NoopClientManager(),
        project_root=project,
        dynamic_mcp_tools=False,
        dynamic_skill_tools=False,
    )

    assert registry.skills["fallback"].source == "managed"
    assert any(
        "project:" in warning and "missing SKILL.md" in warning
        for warning in registry.skill_warnings
    )


@pytest.mark.asyncio
async def test_project_skill_payloads_and_dynamic_reloader_use_registry_snapshot(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    managed = tmp_path / "managed"
    project.mkdir()
    managed.mkdir()
    _install_skill(project, ".agents/skills", "debugging", "find root causes")
    registry = build_agent_registry(
        managed,
        _NoopClientManager(),
        project_root=project,
        dynamic_mcp_tools=False,
        dynamic_skill_tools=True,
    )

    listed = list_agent_skills_payload(registry)
    assert [row["name"] for row in listed.skills] == ["debugging"]
    assert listed.skills[0]["source"] == "project"
    activated = activate_agent_skill_payload(
        registry, "debugging", max_entry_bytes=1024
    )
    assert "find root causes" in activated.content
    related = read_agent_skill_file_payload(
        registry, "debugging", "guide.md", max_file_bytes=1024
    )
    assert related["content"] == "find root causes"
    assert tool_value({"name": "mapping-tool"}, "name") == "mapping-tool"
    with pytest.raises(ValueError, match="Unknown agent skill"):
        activate_agent_skill_payload(registry, "missing")
    with pytest.raises(ValueError, match="Unknown agent skill"):
        read_agent_skill_file_payload(registry, "missing", "guide.md")

    class FakeMcp:
        def __init__(self) -> None:
            self.handlers: dict[str, Any] = {}

        def add_tool(self, handler, *, name, **_kwargs) -> None:
            self.handlers[name] = handler

        def remove_tool(self, name: str) -> None:
            self.handlers.pop(name, None)

    mcp = FakeMcp()
    reloader = AgentBridgeToolReloader(
        mcp,
        registry,
        {},
        probe_timeout_s=1,
        dynamic_mcp_tools=False,
        dynamic_skill_tools=True,
    )
    reloader.register_dynamic_tools()
    handler = mcp.handlers["activate_skill__debugging"]
    dynamic = await handler()
    assert dynamic.name == "debugging"
    assert "find root causes" in dynamic.content


def test_multi_source_scan_shares_entry_and_skill_budgets(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    managed = tmp_path / "managed"
    project.mkdir()
    managed.mkdir()
    _install_skill(project, ".agents/skills", "project-skill", "project")
    _install_skill(managed, "skills", "managed-skill", "managed")
    sources = (
        SkillSource("project", project, ".agents/skills"),
        SkillSource("managed", managed, "skills"),
    )

    by_count = scan_skill_sources(
        sources,
        max_skills=1,
        max_related_files=100,
        max_scan_entries=100,
        max_path_bytes=10_000,
        max_entry_bytes=10_000,
    )

    by_scan = scan_skill_sources(
        sources,
        max_skills=10,
        max_related_files=100,
        max_scan_entries=3,
        max_path_bytes=10_000,
        max_entry_bytes=10_000,
    )
    by_path = scan_skill_sources(
        sources,
        max_skills=10,
        max_related_files=100,
        max_scan_entries=100,
        max_path_bytes=len(b"guide.md"),
        max_entry_bytes=10_000,
    )

    assert list(by_count.skills) == ["project-skill"]
    assert list(by_scan.skills) == ["project-skill"]
    assert any("scan stopped" in warning for warning in by_scan.warnings)
    assert by_path.skills["project-skill"].related_files == ["guide.md"]
    assert by_path.skills["managed-skill"].related_files == []
    assert by_path.path_bytes == len(b"guide.md")
    assert any(
        "path budget is exhausted" in warning for warning in by_path.warnings
    )


@pytest.mark.asyncio
async def test_executor_session_scopes_skill_registry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    workspace = tmp_path / "workspace"
    project = workspace / "project"
    project.mkdir(parents=True)
    managed = tmp_path / "managed"
    managed.mkdir()
    _install_skill(project, ".agents/skills", "executor-skill", "executor")
    _configure(
        monkeypatch,
        workspace=workspace,
        state_dir=tmp_path / "state",
        config_dir=managed,
        xdg_config_home=tmp_path / "xdg",
    )
    runtime = build_executor_runtime(
        get_settings(), enable_control_connection=False
    )
    session_id = "sess_0000000000000000000001"
    runtime.services.tool_session_store.create_session(
        session_id=session_id, workdir=project
    )

    listed = await runtime.dispatcher.execute(
        "list_agent_skills", {"session_id": session_id}
    )
    activated = await runtime.dispatcher.execute(
        "activate_agent_skill",
        {"session_id": session_id, "name": "executor-skill"},
    )
    related = await runtime.dispatcher.execute(
        "read_agent_skill_file",
        {
            "session_id": session_id,
            "name": "executor-skill",
            "path": "guide.md",
        },
    )

    assert [row["name"] for row in listed.skills] == ["executor-skill"]
    assert listed.skills[0]["source"] == "project"
    assert activated.source == "project"
    assert "executor" in activated.content
    assert related.content == "executor"


def test_registry_fingerprint_tracks_project_and_global_sources(
    tmp_path: Path,
) -> None:
    managed = tmp_path / "managed"
    project = tmp_path / "project"
    xdg = tmp_path / "xdg"
    managed.mkdir()
    project.mkdir()
    sources = (
        SkillSource("project", project, ".agents/skills"),
        SkillSource("managed", managed, "skills"),
        SkillSource("global", xdg, "agents/skills"),
    )
    before = agent_registry_fingerprint(managed, sources)
    _install_skill(project, ".agents/skills", "project-skill", "project")
    after_project = agent_registry_fingerprint(managed, sources)
    _install_skill(xdg, "agents/skills", "global-skill", "global")
    after_global = agent_registry_fingerprint(managed, sources)

    assert before != after_project
    assert after_project != after_global
