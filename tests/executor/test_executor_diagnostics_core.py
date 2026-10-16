"""Bounded Executor diagnostic branches: manifest, Skill selection and faults."""

import json

import pytest

from workgate.agent_bridge.models import SkillRecord, SkillScanResult
from workgate.config.executor import resolve_executor_config
from workgate.config.settings import Settings
from workgate.executor import diagnostics


def executor_config(tmp_path):
    home = tmp_path / "workspace"
    home.mkdir()
    return resolve_executor_config(
        Settings(
            state_dir=tmp_path / "state",
            default_workdir=home,
            max_skills=100,
            max_skill_related_files=40,
            max_skill_scan_entries=300,
            max_skill_path_bytes=100_000,
            max_file_read_bytes=70_000,
        )
    )


def write_manifest(config, *, skills=True):
    folder = config.agent_config_dir
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    (folder / "config.json").write_text(
        json.dumps(
            {
                "version": 1,
                "skills": {"enabled": skills},
                "mcpServers": {
                    "stdio-local": {
                        "type": "stdio",
                        "command": "/bin/false",
                        "enabled": False,
                    }
                },
            }
        )
    )


def test_missing_manifest_still_scans_global_skills(tmp_path, monkeypatch):
    config = executor_config(tmp_path)
    global_root = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(global_root))
    skill = global_root / "agents" / "skills" / "my.skill"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# my.skill\n\nA global test Skill.\n")
    result = diagnostics.executor_diagnostics(config)
    assert result["bridge"]["status"] == "missing_config"
    assert result["skills"]["enabled"] is True
    assert result["skills"]["selected"] == [
        {"name": "my.skill", "source": "global"}
    ]
    assert "health" not in result


def test_disabled_skill_manifest_is_not_scanned(tmp_path, monkeypatch):
    config = executor_config(tmp_path)
    write_manifest(config, skills=False)

    def not_called(*_args, **_kwargs):
        raise AssertionError("disabled Skills must not be scanned")

    monkeypatch.setattr(diagnostics, "scan_skill_sources", not_called)
    result = diagnostics.executor_diagnostics(config)
    assert result["bridge"]["status"] == "loaded"
    assert result["bridge"]["servers"] == [
        {"name": "stdio-local", "type": "stdio", "enabled": False}
    ]
    assert result["skills"]["enabled"] is False
    assert result["skills"]["selected"] == []


def test_skill_scan_names_warning_duplicates_and_budgets(tmp_path, monkeypatch):
    config = executor_config(tmp_path)
    write_manifest(config)
    observed = {}

    def scanned(sources, **budgets):
        observed.update(budgets)
        assert [source.name for source in sources] == ["managed", "global"]
        return SkillScanResult(
            skills={
                "hello": SkillRecord(
                    name="hello",
                    source="managed",
                    source_path="/secret/path",
                    entry_path="SKILL.md",
                    description="Secret contents must not escape",
                    related_files=["private-token-file"],
                )
            },
            overridden=[("hello", "global")],
            warnings=[
                "global: skipped duplicate Skill 'hello'; a higher-priority source already provides it",
                "invalid entry",
            ],
        )

    monkeypatch.setattr(diagnostics, "scan_skill_sources", scanned)
    result = diagnostics.executor_diagnostics(config)
    assert observed["max_skills"] == 32
    assert observed["max_related_files"] == 4
    assert observed["max_scan_entries"] == 128
    assert observed["max_path_bytes"] == 65_536
    assert observed["max_entry_bytes"] == 4096
    assert result["skills"]["enabled"] is True
    assert result["skills"]["selected"] == [
        {"name": "hello", "source": "managed"}
    ]
    assert result["skills"]["duplicates"] == 1
    assert result["skills"]["overridden"] == [
        {"name": "hello", "source": "global"}
    ]
    assert result["skills"]["warnings"] == 2
    assert "Secret contents" not in json.dumps(result)
    assert "/secret/path" not in json.dumps(result)


@pytest.mark.parametrize(
    "exception", [OSError("unreadable"), ValueError("invalid")]
)
def test_skill_source_errors_are_safe_and_do_not_hide_manifest(
    tmp_path, monkeypatch, exception
):
    config = executor_config(tmp_path)
    write_manifest(config)

    def fail(*_args, **_kwargs):
        raise exception

    monkeypatch.setattr(diagnostics, "scan_skill_sources", fail)
    result = diagnostics.executor_diagnostics(config)
    assert result["bridge"]["status"] == "loaded"
    assert result["skills"]["enabled"] is False
    assert result["skills"]["warnings"] == 1
    assert "unreadable" not in json.dumps(result)
    assert "invalid" not in json.dumps(result)


def test_structured_overrides_accept_valid_non_alphanumeric_names(
    tmp_path, monkeypatch
):
    config = executor_config(tmp_path)
    write_manifest(config)
    home = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home))
    for name in ("my.skill", "my skill", "测试技能"):
        for root in (
            config.agent_config_dir / "skills",
            home / "agents" / "skills",
        ):
            directory = root / name
            directory.mkdir(parents=True)
            (directory / "SKILL.md").write_text(f"# {name}\n\nExample Skill.\n")
    result = diagnostics.executor_diagnostics(config)["skills"]
    assert {item["name"] for item in result["selected"]} == {
        "my.skill",
        "my skill",
        "测试技能",
    }
    assert {item["name"] for item in result["overridden"]} == {
        "my.skill",
        "my skill",
        "测试技能",
    }
    assert {item["source"] for item in result["overridden"]} == {"global"}
