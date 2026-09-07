from pathlib import Path

from workgate.config.settings import Settings
from workgate.control.config import resolve_control_config
from workgate.control.runtime import build_control_runtime
from workgate.executor.config import resolve_executor_config
from workgate.executor.runtime import build_executor_runtime


def test_role_configs_expose_only_their_authority(tmp_path: Path) -> None:
    settings = Settings(
        workspace_root=tmp_path / "workspace",
        state_dir=tmp_path / "state",
        host="127.0.0.2",
        port=9876,
        command_denylist=["shutdown"],
        path_denylist=[".env"],
        executor_max_pending_commands=17,
    )

    control = resolve_control_config(settings)
    executor = resolve_executor_config(settings)

    assert control.host == "127.0.0.2"
    assert control.port == 9876
    assert control.state_dir == settings.state_dir.resolve(strict=False)
    assert control.executor_max_pending_commands == 17
    assert not hasattr(control, "workspace_root")
    assert not hasattr(control, "command_denylist")
    assert not hasattr(control, "shell_executable")

    assert executor.workspace_root == settings.workspace_root.resolve(
        strict=False
    )
    assert executor.command_denylist == ("shutdown",)
    assert executor.path_denylist == (".env",)
    assert not hasattr(executor, "host")
    assert not hasattr(executor, "port")
    assert not hasattr(executor, "auth_mode")
    assert not hasattr(executor, "state_dir")
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
        mode="http",
    )
    executor_settings = Settings(
        workspace_root=tmp_path / "executor-workspace",
        state_dir=tmp_path / "executor-legacy-state",
    )

    control = build_control_runtime(control_settings)
    executor = build_executor_runtime(executor_settings)

    assert control.config.mode == "http"
    assert control.legacy_settings is control_settings
    assert (
        executor.config.workspace_root
        == executor_settings.workspace_root.resolve(strict=False)
    )
    assert executor.legacy_settings is executor_settings
