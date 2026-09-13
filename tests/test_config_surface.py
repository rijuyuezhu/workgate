import argparse
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Annotated, Any, Literal

import pytest
import yaml

import workgate.config.settings as settings_module
import workgate.config.surface as surface
from workgate.config.settings import Settings, load_settings
from workgate.config.surface import (
    SETTING_SPECS,
    SPECS_BY_NAME,
    SettingSpec,
)


@dataclass(frozen=True)
class FakeField:
    annotation: Any


def test_path_defaults_use_portable_posix_separators():
    value = PureWindowsPath(r"\workspace\.workgate")

    assert surface.default_to_string(value) == "/workspace/.workgate"
    assert surface.yaml_default(value) == "/workspace/.workgate"


def test_surface_helpers_cover_passthrough_and_failure_paths(monkeypatch):
    assert surface.yaml_default("literal") == "literal"

    action = surface.BoolChoiceAction(["--flag"], "flag")
    with pytest.raises(
        argparse.ArgumentTypeError, match="Invalid boolean value"
    ):
        action(argparse.ArgumentParser(), argparse.Namespace(), "invalid")

    incomplete_specs = dict(surface.SPECS_BY_NAME)
    incomplete_specs.pop("port")
    monkeypatch.setattr(surface, "SPECS_BY_NAME", incomplete_specs)
    with pytest.raises(RuntimeError, match="Setting spec mismatch"):
        surface.validate_setting_specs()


def test_generated_yaml_example_loads_without_losing_defaults(monkeypatch):
    for spec in SETTING_SPECS:
        monkeypatch.delenv(spec.env_var, raising=False)
    monkeypatch.delenv("WORKGATE_CONFIG", raising=False)

    settings = load_settings("config.example.yaml")
    defaults = Settings()

    assert settings.command_denylist == defaults.command_denylist
    assert settings.path_denylist == defaults.path_denylist
    assert settings.allow_full_control == defaults.allow_full_control


def test_setting_spec_properties_cover_current_setting_shapes():
    assert SPECS_BY_NAME["port"].argparse_type is int
    assert SPECS_BY_NAME["tool_timeout_s"].argparse_type is float
    assert SPECS_BY_NAME["host"].argparse_type is str
    assert SPECS_BY_NAME["base_url"].argparse_type is str

    assert SPECS_BY_NAME["mode"].choices == ("mcp", "http", "both", "stdio")
    assert SPECS_BY_NAME["host"].choices is None

    assert not SPECS_BY_NAME["base_url"].is_bool
    assert SPECS_BY_NAME["base_url"].is_nullable
    assert not SPECS_BY_NAME["command_denylist"].is_nullable


def test_setting_spec_rejects_unregistered_setting() -> None:
    with pytest.raises(ValueError, match="Setting name not found"):
        SettingSpec("unregistered_setting", "Server")


def test_setting_spec_properties_cover_future_nullable_shapes(monkeypatch):
    monkeypatch.setattr(
        surface.Settings,
        "model_fields",
        {
            "optional_int": FakeField(int | None),
            "optional_float": FakeField(float | None),
            "optional_bool": FakeField(bool | None),
            "optional_literal": FakeField(Literal["alpha", "beta"] | None),
            "annotated_optional_int": FakeField(
                Annotated[int | None, "metadata"]
            ),
            "annotated_optional_literal": FakeField(
                Annotated[Literal["one", "two"] | None, "metadata"]
            ),
            "annotated_list": FakeField(Annotated[list[str], "metadata"]),
            "wide_optional": FakeField(int | str | None),
        },
    )

    specs = {
        name: SettingSpec(name, "Server")
        for name in surface.Settings.model_fields
    }

    assert specs["optional_int"].argparse_type is int
    assert specs["optional_float"].argparse_type is float
    assert specs["optional_bool"].argparse_type is str
    assert specs["optional_literal"].argparse_type is str
    assert specs["annotated_optional_int"].argparse_type is int
    assert specs["wide_optional"].argparse_type is str

    assert specs["optional_bool"].choices == ("true", "false")
    assert specs["optional_literal"].choices == ("alpha", "beta")
    assert specs["annotated_optional_literal"].choices == ("one", "two")
    assert specs["annotated_list"].choices is None
    assert specs["wide_optional"].choices is None

    assert specs["optional_bool"].is_bool
    assert not specs["optional_int"].is_bool
    assert specs["optional_int"].is_nullable
    assert specs["annotated_optional_int"].is_nullable
    assert specs["wide_optional"].is_nullable
    assert not specs["annotated_list"].is_nullable


def test_config_file_errors(tmp_path):
    assert settings_module._split_csv(None) == []

    missing = tmp_path / "missing.yaml"
    with pytest.raises(FileNotFoundError):
        settings_module.read_config_file(missing)
    empty = tmp_path / "empty.yaml"
    empty.write_text("", encoding="utf-8")
    assert settings_module.read_config_file(empty) == {}

    nullable_path = tmp_path / "nullable-path.yaml"
    nullable_path.write_text("workspace_root: null\n", encoding="utf-8")
    assert settings_module.read_config_file(nullable_path) == {
        "workspace_root": None
    }


def test_settings_expose_platform_owned_namespaces() -> None:
    settings = Settings()
    paths = settings_module.app_paths()

    assert settings.config_dir == paths.config_dir
    assert settings.data_dir == paths.data_dir
    assert settings.cache_dir == paths.cache_dir


def test_data_dir_can_be_overridden_without_platform_specific_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    configured = tmp_path / "persistent-data"
    monkeypatch.setenv("WORKGATE_DATA_DIR", str(configured))

    assert load_settings().data_dir == configured.resolve()


def test_configured_data_dir_must_be_absolute(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text('data_dir: "./relative-data"\n', encoding="utf-8")

    with pytest.raises(ValueError, match="data_dir must be an absolute path"):
        load_settings(config)


def test_workspace_defaults_to_invocation_cwd(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("WORKGATE_WORKSPACE_ROOT", raising=False)
    monkeypatch.delenv("WORKGATE_CONFIG", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config-home"))

    assert load_settings().workspace_root == tmp_path.resolve()


def test_default_config_is_discovered_from_platform_config_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config_dir = settings_module.app_paths().config_dir
    config_dir.mkdir(parents=True)
    workspace = tmp_path / "configured-workspace"
    (config_dir / "config.yaml").write_text(
        yaml.safe_dump({"workspace_root": str(workspace), "port": 8123}),
        encoding="utf-8",
    )
    monkeypatch.delenv("WORKGATE_CONFIG", raising=False)
    monkeypatch.delenv("WORKGATE_WORKSPACE_ROOT", raising=False)

    settings = load_settings()

    assert settings.workspace_root == workspace
    assert settings.port == 8123


def test_worker_runtime_does_not_discover_ambient_default_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config_dir = settings_module.app_paths().config_dir
    config_dir.mkdir(parents=True)
    ambient_workspace = tmp_path / "ambient-workspace"
    (config_dir / "config.yaml").write_text(
        yaml.safe_dump(
            {"workspace_root": str(ambient_workspace), "port": 8123}
        ),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("WORKGATE_CONFIG", raising=False)
    monkeypatch.delenv("WORKGATE_WORKSPACE_ROOT", raising=False)
    monkeypatch.setenv("WORKGATE_REMOTE_WORKER_RUNTIME", "1")

    settings = load_settings()

    assert settings.workspace_root == tmp_path.resolve()
    assert settings.port != 8123


def test_worker_runtime_honors_explicit_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = tmp_path / "worker.yaml"
    workspace = tmp_path / "configured-workspace"
    config.write_text(
        yaml.safe_dump({"workspace_root": str(workspace), "port": 8123}),
        encoding="utf-8",
    )
    monkeypatch.setenv("WORKGATE_REMOTE_WORKER_RUNTIME", "1")
    monkeypatch.setenv("WORKGATE_CONFIG", str(config))
    monkeypatch.delenv("WORKGATE_WORKSPACE_ROOT", raising=False)

    settings = load_settings()

    assert settings.workspace_root == workspace
    assert settings.port == 8123


def test_yaml_paths_must_be_absolute_after_expansion(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("WORKGATE_WORKSPACE_ROOT", raising=False)
    config = tmp_path / "config.yaml"
    config.write_text('workspace_root: "./relative"\n', encoding="utf-8")

    with pytest.raises(
        ValueError, match="workspace_root must be an absolute path"
    ):
        load_settings(config)

    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    config.write_text('workspace_root: "~/project"\n', encoding="utf-8")
    assert load_settings(config).workspace_root == home / "project"


def test_relative_env_and_cli_paths_remain_invocation_relative(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config-home"))
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", "env-workspace")

    assert load_settings().workspace_root == tmp_path / "env-workspace"
    assert load_settings(
        overrides={"workspace_root": "cli-workspace"}
    ).workspace_root == (tmp_path / "cli-workspace")
