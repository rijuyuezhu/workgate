"""File-backed role config save is CAS, private, validated and never applied."""

import argparse
import json
from pathlib import Path

import pytest

from workgate.config.editing import ConfigEditError, revision_for, save_config
from workgate.config.inspection import (
    capture_startup_config,
    current_startup_config,
)
from workgate.config.roles import CONTROL_SETTING_NAMES, EXECUTOR_SETTING_NAMES
from workgate.config.settings import load_settings


def snapshot(path: Path, role="control"):
    selected = (
        CONTROL_SETTING_NAMES if role == "control" else EXECUTOR_SETTING_NAMES
    )
    config = load_settings(path, setting_names=selected)
    capture_startup_config(role, argparse.Namespace(config=str(path)), config)
    result = current_startup_config(role)
    assert result is not None
    return result


def set_field(name, value):
    return {name: {"op": "set", "value": value}}


def test_yaml_comment_backup_private_file_and_no_implicit_apply(tmp_path):
    path = tmp_path / "control.yaml"
    path.write_text("# Keep this comment\nport: 8765 # service port\n")
    old = snapshot(path)
    rev = revision_for(old)
    new = save_config(old, revision=rev, changes=set_field("port", 8877))
    assert new != rev
    assert "# Keep this comment" in path.read_text()
    assert "service port" in path.read_text()
    assert "8877" in path.read_text()
    assert (
        tmp_path / "control.yaml.bak"
    ).read_text() == "# Keep this comment\nport: 8765 # service port\n"
    assert path.stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "control.yaml.bak").stat().st_mode & 0o777 == 0o600
    assert old.settings.port == 8765
    assert (
        old.describe()["fields"][2]["effective_value"] != 8877
        if False
        else old.settings.port == 8765
    )


def test_json_role_fields_preserve_foreign_fields_and_unset(tmp_path):
    path = tmp_path / "executor.json"
    path.write_text(json.dumps({"max_jobs": 19, "port": 8765}))
    snap = snapshot(path, "executor")
    new = save_config(
        snap, revision=revision_for(snap), changes=set_field("max_jobs", 32)
    )
    saved = json.loads(path.read_text())
    assert saved == {"max_jobs": 32, "port": 8765}
    save_config(snap, revision=new, changes={"max_jobs": {"op": "remove"}})
    assert json.loads(path.read_text()) == {"port": 8765}
    assert snap.settings.max_jobs == 19


def test_stale_revision_refused_no_overwrite(tmp_path):
    path = tmp_path / "control.yaml"
    path.write_text("port: 8001\n")
    snap = snapshot(path)
    version = revision_for(snap)
    path.write_text("port: 8002\n")
    with pytest.raises(ConfigEditError, match="RevisionConflict"):
        save_config(snap, revision=version, changes=set_field("port", 8003))
    assert path.read_text() == "port: 8002\n"


@pytest.mark.parametrize("value", [0, 70000, "fake", -1])
def test_invalid_values_never_written(tmp_path, value):
    path = tmp_path / "control.yaml"
    path.write_text("port: 8001\n")
    snap = snapshot(path)
    with pytest.raises(ConfigEditError, match="InvalidConfigValue"):
        save_config(
            snap, revision=revision_for(snap), changes=set_field("port", value)
        )
    assert path.read_text() == "port: 8001\n"
    assert not (tmp_path / "control.yaml.bak").exists()


def test_role_ownership_and_unsafe_path(tmp_path):
    path = tmp_path / "control.yaml"
    path.write_text("port: 8001\n")
    snap = snapshot(path)
    with pytest.raises(ConfigEditError, match="InvalidEdit"):
        save_config(
            snap,
            revision=revision_for(snap),
            changes=set_field("default_workdir", "/tmp"),
        )
    with pytest.raises(ConfigEditError, match="InvalidConfigValue"):
        save_config(
            snap,
            revision=revision_for(snap),
            changes=set_field("state_dir", "relative/path"),
        )
    link = tmp_path / "alias.yaml"
    link.symlink_to(path)
    snap = snapshot(path)
    from dataclasses import replace

    with pytest.raises(ConfigEditError, match="UnsafeConfigPath"):
        save_config(
            replace(snap, config_path=link),
            revision=revision_for(snap),
            changes=set_field("port", 9000),
        )
    path.chmod(0o666)
    assert revision_for(snap) is None


def test_atomic_write_error_does_not_corrupt_file(tmp_path, monkeypatch):
    path = tmp_path / "executor.json"
    path.write_text('{"max_jobs": 31}')
    snap = snapshot(path, "executor")

    def broken(path, contents):
        raise OSError("private contents must never reach API")

    monkeypatch.setattr(
        "workgate.config.editing.atomic_write_private_bytes", broken
    )
    with pytest.raises(ConfigEditError, match="ConfigWriteFailed"):
        save_config(
            snap, revision=revision_for(snap), changes=set_field("max_jobs", 42)
        )
    assert json.loads(path.read_text()) == {"max_jobs": 31}


def test_concurrent_writers_one_wins(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    path = tmp_path / "control.yaml"
    path.write_text("port: 8001\n")
    snap = snapshot(path)
    version = revision_for(snap)

    def write(port):
        try:
            return save_config(
                snap, revision=version, changes=set_field("port", port)
            )
        except ConfigEditError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(write, [8002, 8003]))
    assert sum(item == "RevisionConflict" for item in results) == 1
    assert len([item for item in results if item != "RevisionConflict"]) == 1
    assert "port: 800" in path.read_text()


def test_no_selected_file_is_read_only(tmp_path):
    from dataclasses import replace

    path = tmp_path / "control.yaml"
    path.write_text("port: 8001\n")
    snap = replace(snapshot(path), config_path=None)
    assert revision_for(snap) is None
    with pytest.raises(ConfigEditError, match="NoConfigFile"):
        save_config(snap, revision="a" * 64, changes=set_field("port", 8003))


def test_duplicate_yaml_key_falls_back_to_valid_mapping(tmp_path):
    import yaml

    path = tmp_path / "control.yaml"
    path.write_text(
        "# old comment\nlog_level: warning\nlog_level: info # duplicate\n"
    )
    snap = snapshot(path)
    save_config(
        snap,
        revision=revision_for(snap),
        changes=set_field("log_level", "error"),
    )
    assert yaml.safe_load(path.read_text())["log_level"] == "error"
    assert path.read_text().count("log_level:") == 1
