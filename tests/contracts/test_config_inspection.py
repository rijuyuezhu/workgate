"""Live Child config precedence and admin-only secret-bearing projections."""

import argparse
import json

from workgate.config.inspection import (
    capture_startup_config,
    current_startup_config,
)
from workgate.config.roles import CONTROL_SETTING_NAMES, EXECUTOR_SETTING_NAMES
from workgate.config.settings import load_settings
from workgate.oauth.core.scopes import (
    default_scope,
    normalize_requested_scope,
    supported_scopes,
)


def _capture(role, path, *, cli=None, default=None):
    names = (
        CONTROL_SETTING_NAMES if role == "control" else EXECUTOR_SETTING_NAMES
    )
    args = argparse.Namespace(
        config=str(path), _workgate_default_config_path=default
    )
    for name, value in (cli or {}).items():
        setattr(args, name, value)
    settings = load_settings(
        path,
        cli or {},
        setting_names=names,
    )
    capture_startup_config(role, args, settings)
    result = current_startup_config(role)
    assert result is not None
    return result


def test_control_provenance_and_file_changes_do_not_change_live_settings(
    tmp_path, monkeypatch
):
    path = tmp_path / "control.yaml"
    path.write_text(
        "port: 8001\noauth_admin_pin: private-pin\nlog_level: info\n"
    )
    monkeypatch.setenv("WORKGATE_PORT", "8002")
    snapshot = _capture("control", path, cli={"port": 8003})
    view = snapshot.describe()
    rows = {field["name"]: field for field in view["fields"]}
    assert rows["port"]["effective_value"] == 8003
    assert rows["port"]["source"] == "cli"
    assert rows["port"]["file_value"] == 8001
    assert rows["port"]["file_overridden"]
    assert rows["log_level"]["source"] == "file"
    assert rows["oauth_admin_pin"]["effective_value"] == "private-pin"
    assert "shell_executable" not in rows
    assert not view["file_changed_since_startup"]
    path.write_text("port: 8009\noauth_admin_pin: changed\n")
    again = snapshot.describe()
    assert again["file_changed_since_startup"]
    again_rows = {field["name"]: field for field in again["fields"]}
    assert again_rows["port"]["effective_value"] == 8003
    assert again_rows["port"]["file_value"] == 8009
    assert again_rows["oauth_admin_pin"]["effective_value"] == "private-pin"
    assert again_rows["oauth_admin_pin"]["file_value"] == "changed"


def test_executor_service_json_uses_actual_selection(tmp_path, monkeypatch):
    monkeypatch.delenv("WORKGATE_PORT", raising=False)
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({"max_jobs": 60, "default_workdir": str(tmp_path)})
    )
    snapshot = _capture("executor", path)
    view = snapshot.describe()
    rows = {field["name"]: field for field in view["fields"]}
    assert view["config_path"] == str(path)
    assert view["config_format"] == "json"
    assert rows["max_jobs"]["effective_value"] == 60
    assert rows["max_jobs"]["source"] == "file"
    assert "oauth_admin_pin" not in rows
    assert "port" not in rows


def test_config_uses_existing_scope_model():
    assert "config:admin" not in supported_scopes()
    assert "shell:read" in default_scope().split()
    assert "executor:use" in normalize_requested_scope(None).split()


def test_env_overrides_file_and_restart_resnapshots(tmp_path, monkeypatch):
    file = tmp_path / "role.json"
    file.write_text('{"max_jobs": 24}')
    monkeypatch.setenv("WORKGATE_MAX_JOBS", "32")
    snapshot = _capture("executor", file)
    field = {item["name"]: item for item in snapshot.describe()["fields"]}[
        "max_jobs"
    ]
    assert (field["source"], field["effective_value"], field["file_value"]) == (
        "env",
        32,
        24,
    )
    assert field["file_overridden"]
    file.write_text('{"max_jobs": 64}')
    assert snapshot.describe()["file_changed_since_startup"]
    assert field["effective_value"] == 32
    monkeypatch.delenv("WORKGATE_MAX_JOBS")
    restarted = _capture("executor", file)
    current = {item["name"]: item for item in restarted.describe()["fields"]}[
        "max_jobs"
    ]
    assert (current["source"], current["effective_value"]) == ("file", 64)
    assert not restarted.describe()["file_changed_since_startup"]


def test_invalid_file_after_start_does_not_leak_content_or_throw(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("WORKGATE_MAX_JOBS", raising=False)
    file = tmp_path / "role.yaml"
    file.write_text("max_jobs: 51\n")
    snapshot = _capture("executor", file)
    file.write_text("max_jobs: [super-secret-invalid-yaml\n")
    result = snapshot.describe()
    assert (
        result["file_error"] == "Configuration file is unavailable or invalid"
    )
    assert "super-secret-invalid-yaml" not in str(result)
    assert result["file_changed_since_startup"]
    current = {item["name"]: item for item in result["fields"]}["max_jobs"]
    assert current["effective_value"] == 51
    assert not current["file_present"]


def test_relative_explicit_path_is_resolved_at_startup(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = tmp_path / "custom.yaml"
    config.write_text("port: 8766\n")
    snapshot = _capture("control", "custom.yaml")
    assert snapshot.describe()["config_path"] == str(config)
    monkeypatch.chdir(tmp_path.parent)
    assert snapshot.describe()["config_path"] == str(config)


def test_non_json_yaml_value_after_start_is_presented_safely(tmp_path):
    file = tmp_path / "executor.yaml"
    file.write_text("max_jobs: 51\n")
    snapshot = _capture("executor", file)
    file.write_text("max_jobs: .nan\n")
    result = snapshot.describe()
    field = {item["name"]: item for item in result["fields"]}["max_jobs"]
    assert field["effective_value"] == 51
    assert field["file_value"] == "nan"
    import json

    json.dumps(result, allow_nan=False)  # Strict JSONResponse compatibility.


def test_environment_selected_config_and_setting_schema_parity(
    tmp_path, monkeypatch
):
    path = tmp_path / "relocated.yaml"
    path.write_text("port: 8987\n")
    monkeypatch.setenv("WORKGATE_CONFIG", str(path))
    args = argparse.Namespace(config=None)
    settings = load_settings(setting_names=CONTROL_SETTING_NAMES)
    capture_startup_config("control", args, settings)
    snapshot = current_startup_config("control")
    assert snapshot is not None
    data = snapshot.describe()
    assert data["config_path"] == str(path)
    assert len(data["fields"]) == len(CONTROL_SETTING_NAMES)
    assert {row["name"] for row in data["fields"]} == CONTROL_SETTING_NAMES
    before = path.stat().st_mtime_ns
    assert snapshot.describe()["startup_at"] == data["startup_at"]
    assert path.stat().st_mtime_ns == before  # Read-only, no implicit Apply.
