from pathlib import Path

import pytest

from workgate.config.control import get_control_config, resolve_control_config
from workgate.config.executor import (
    get_executor_config,
    resolve_executor_config,
)
from workgate.config.role_config import use_role_config
from workgate.config.settings import Settings
from workgate.control.runtime import build_control_runtime
from workgate.executor.runtime import build_executor_runtime


def test_role_configs_expose_only_their_authority(tmp_path: Path) -> None:
    settings = Settings(
        workspace_root=tmp_path / "workspace",
        state_dir=tmp_path / "state",
        data_dir=tmp_path / "data",
        host="127.0.0.2",
        port=9876,
        command_denylist=["shutdown"],
        path_denylist=[".env"],
        ui_terminal_max_connections=23,
        executor_max_pending_commands=17,
    )

    control = resolve_control_config(settings)
    executor = resolve_executor_config(settings)

    assert control.host == "127.0.0.2"
    assert control.port == 9876
    assert control.state_dir == settings.state_dir.resolve(strict=False)
    assert control.data_dir == settings.data_dir.resolve(strict=False)
    assert control.ui_terminal_max_connections == 23
    assert control.executor_max_pending_commands == 17
    assert not hasattr(control, "workspace_root")
    assert not hasattr(control, "command_denylist")
    assert not hasattr(control, "shell_executable")

    assert executor.workspace_root == settings.workspace_root.resolve(
        strict=False
    )
    assert executor.command_denylist == ("shutdown",)
    assert executor.path_denylist == (".env",)
    assert executor.state_dir == settings.state_dir.resolve(strict=False)
    assert not hasattr(executor, "host")
    assert not hasattr(executor, "port")
    assert not hasattr(executor, "auth_mode")
    assert not hasattr(executor, "executor_max_pending_commands")


def test_role_configs_snapshot_legacy_settings(tmp_path: Path) -> None:
    settings = Settings(
        workspace_root=tmp_path,
        state_dir=tmp_path / "state",
        command_denylist=["shutdown"],
    )
    executor = resolve_executor_config(settings)

    settings.command_denylist.append("reboot")

    assert executor.command_denylist == ("shutdown",)


def test_runtime_roots_carry_explicit_role_config(tmp_path: Path) -> None:
    control_settings = Settings(
        workspace_root=tmp_path / "control-workspace",
        state_dir=tmp_path / "control-state",
        data_dir=tmp_path / "control-data",
        mode="http",
    )
    executor_settings = Settings(
        workspace_root=tmp_path / "executor-workspace",
        state_dir=tmp_path / "executor-legacy-state",
    )

    control = build_control_runtime(control_settings)
    executor = build_executor_runtime(
        resolve_executor_config(executor_settings)
    )

    assert control.config.mode == "http"
    assert not hasattr(control, "legacy_settings")
    assert control.config.state_dir == control_settings.state_dir.resolve(
        strict=False
    )
    assert control.config.data_dir == control_settings.data_dir.resolve(
        strict=False
    )
    assert (
        executor.config.workspace_root
        == executor_settings.workspace_root.resolve(strict=False)
    )
    assert not hasattr(executor, "legacy_settings")


def test_executor_config_context_is_explicit_and_role_checked(
    tmp_path: Path,
) -> None:
    settings = Settings(
        workspace_root=tmp_path / "workspace",
        state_dir=tmp_path / "state",
    )
    executor = resolve_executor_config(settings)
    control = resolve_control_config(settings)

    with pytest.raises(RuntimeError, match="not configured"):
        get_executor_config()

    with (
        use_role_config(control),
        pytest.raises(RuntimeError, match="not executor-owned"),
    ):
        get_executor_config()

    with use_role_config(executor):
        assert get_executor_config() is executor


def test_control_config_context_is_role_checked(tmp_path: Path) -> None:
    settings = Settings(
        workspace_root=tmp_path / "workspace",
        state_dir=tmp_path / "state",
    )
    control = resolve_control_config(settings)
    executor = resolve_executor_config(settings)

    with use_role_config(control):
        assert get_control_config() is control

    with (
        use_role_config(executor),
        pytest.raises(RuntimeError, match="not control-owned"),
    ):
        get_control_config()
