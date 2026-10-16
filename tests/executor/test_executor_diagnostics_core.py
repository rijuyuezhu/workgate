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


def test_missing_manifest_and_degraded_resource_sampling(tmp_path, monkeypatch):
    config = executor_config(tmp_path)
    monkeypatch.setattr(
        diagnostics,
        "dashboard_snapshot",
        lambda _config: {
            "system": {
                "cpu_percent": 1,
                "memory_percent": None,
                "disk_percent": "bad",
                "disk_total_bytes": 200,
            }
        },
    )
    result = diagnostics.executor_diagnostics(config)
    assert result["bridge"]["status"] == "missing_config"
    assert result["skills"]["enabled"] is False
    assert result["skills"]["selected"] == []
    assert result["health"] == {"cpu_percent": 1, "disk_total_bytes": 200}

    def fail(_config):
        raise OSError("collector disconnected")

    monkeypatch.setattr(diagnostics, "dashboard_snapshot", fail)
    assert diagnostics.executor_diagnostics(config)["health"] == {}


def test_disabled_skill_manifest_is_not_scanned(tmp_path, monkeypatch):
    config = executor_config(tmp_path)
    write_manifest(config, skills=False)

    def not_called(*_args, **_kwargs):
        raise AssertionError("disabled Skills must not be scanned")

    monkeypatch.setattr(diagnostics, "scan_skill_sources", not_called)
    monkeypatch.setattr(
        diagnostics, "dashboard_snapshot", lambda _config: {"system": {}}
    )
    result = diagnostics.executor_diagnostics(config)
    assert result["bridge"]["status"] == "loaded"
    assert result["bridge"]["servers"] == [
        {"name": "stdio-local", "type": "stdio", "enabled": False}
    ]
    assert result["skills"]["enabled"] is False
    assert len(result["skills"]["sources"]) == 2
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
            warnings=[
                "global: skipped duplicate Skill 'hello'; a higher-priority source already provides it",
                "invalid entry",
            ],
        )

    monkeypatch.setattr(diagnostics, "scan_skill_sources", scanned)
    monkeypatch.setattr(
        diagnostics,
        "dashboard_snapshot",
        lambda _config: {"system": {"memory_percent": 23.5}},
    )
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
    assert result["skills"]["overridden"] == [{"name": "hello", "source": "global"}]
    assert result["skills"]["warnings"] == 2
    assert result["health"] == {"memory_percent": 23.5}
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
    monkeypatch.setattr(
        diagnostics, "dashboard_snapshot", lambda _config: {"system": {}}
    )
    result = diagnostics.executor_diagnostics(config)
    assert result["bridge"]["status"] == "loaded"
    assert result["skills"]["enabled"] is False
    assert result["skills"]["warnings"] == 1
    assert "unreadable" not in json.dumps(result)
    assert "invalid" not in json.dumps(result)
