"""Install the ordinary colocate launcher as a Linux systemd user service."""

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

from ..app_paths import app_paths
from ..config.executor import EXECUTOR_SETTING_NAMES
from ..config.settings import load_settings
from ..executor.profile import ExecutorProfileStore
from ..persistence import FileStateStore
from ..utils.private_files import atomic_write_private_text

_SERVICE_NAME = "workgate-colocate.service"
_MANAGED_HEADER = "# Managed by workgate colocate install-service"


def _systemctl(*arguments: str) -> None:
    command = ["systemctl", "--user", *arguments]
    result = subprocess.run(
        command, capture_output=True, text=True, check=False, timeout=15
    )
    if result.returncode:
        raise RuntimeError(
            f"{' '.join(command)} failed: "
            f"{(result.stderr or result.stdout).strip() or result.returncode}"
        )


def _quoted(argument: str) -> str:
    """Quote one literal systemd ExecStart argument (without a shell)."""
    escaped = (
        argument.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("%", "%%")
        .replace("$", "$$")
    )
    return f'"{escaped}"'


def _config_file(value: str | None) -> Path | None:
    if value is None:
        return None
    path = Path(value).expanduser().absolute()
    if not path.is_file():
        raise FileNotFoundError(path)
    return path


def _unit_path() -> Path:
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", ""))
    if not config_home.is_absolute():
        config_home = Path.home() / ".config"
    return config_home / "systemd" / "user" / _SERVICE_NAME


def install_colocate_service(args: argparse.Namespace) -> None:
    """Create and start one systemd user unit, without introducing runtime state."""
    if sys.platform != "linux" or shutil.which("systemctl") is None:
        raise RuntimeError(
            "colocate install-service requires Linux systemd --user"
        )
    if any(name.startswith("WORKGATE_") for name in os.environ):
        raise RuntimeError(
            "colocate install-service requires durable YAML config, "
            "not WORKGATE_* environment overrides"
        )
    _systemctl("show-environment")

    control_config = _config_file(args.control_config)
    executor_config = _config_file(args.executor_config)
    paths = app_paths()
    settings = load_settings(
        executor_config,
        setting_names=EXECUTOR_SETTING_NAMES,
        default_config_path=paths.executor_config_file,
        default_overrides={"state_dir": paths.executor_state_dir},
    )
    if (
        ExecutorProfileStore(FileStateStore(lambda: settings.state_dir)).load()
        is None
    ):
        raise RuntimeError(
            "pair the executor before installing colocate service"
        )

    executable = str(Path(sys.executable).absolute())
    command = (
        [executable, "colocate"]
        if getattr(sys, "frozen", False)
        else [executable, "-m", "workgate.main", "colocate"]
    )
    if control_config is not None:
        command.extend(["--control-config", str(control_config)])
    if executor_config is not None:
        command.extend(["--executor-config", str(executor_config)])

    unit = _unit_path()
    if unit.is_symlink() or (
        unit.exists() and not unit.read_text().startswith(_MANAGED_HEADER)
    ):
        raise RuntimeError(f"refusing to overwrite unmanaged service: {unit}")
    content = "\n".join(
        [
            _MANAGED_HEADER,
            "[Unit]",
            "Description=Workgate colocated control and executor",
            "After=network-online.target",
            "Wants=network-online.target",
            "",
            "[Service]",
            "Type=simple",
            "UMask=0077",
            "ExecStart=" + " ".join(_quoted(part) for part in command),
            "Restart=on-failure",
            "RestartSec=5",
            "",
            "[Install]",
            "WantedBy=default.target",
            "",
        ]
    )
    atomic_write_private_text(unit, content)
    _systemctl("daemon-reload")
    _systemctl("enable", _SERVICE_NAME)
    _systemctl("restart", _SERVICE_NAME)
    print(f"Installed and started {_SERVICE_NAME} ({unit})")
