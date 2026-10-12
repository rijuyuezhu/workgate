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
from ..utils.systemd import systemd_quote, systemd_user_unit_path

_SERVICE_NAME = "workgate-colocate.service"
_MANAGED_HEADER = "# Managed by workgate colocate install-service"


def _systemctl(*arguments: str) -> subprocess.CompletedProcess[str]:
    command = ["systemctl", "--user", *arguments]
    result = subprocess.run(
        command, capture_output=True, text=True, check=False, timeout=15
    )
    if result.returncode:
        raise RuntimeError(
            f"{' '.join(command)} failed: "
            f"{(result.stderr or result.stdout).strip() or result.returncode}"
        )
    return result


def _config_file(value: str | None) -> Path | None:
    if value is None:
        return None
    path = Path(value).expanduser().absolute()
    if not path.is_file():
        raise FileNotFoundError(path)
    return path


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

    paths = app_paths()
    control_config = _config_file(args.control_config)
    executor_config = _config_file(args.executor_config)
    if control_config is None and paths.config_file.is_file():
        control_config = paths.config_file
    if executor_config is None and paths.executor_config_file.is_file():
        executor_config = paths.executor_config_file
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

    try:
        unit = systemd_user_unit_path(
            _systemctl("show", "--property=UnitPath", "--value").stdout,
            _SERVICE_NAME,
        )
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc
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
            "ExecStart=" + " ".join(systemd_quote(part) for part in command),
            "Restart=always",
            "RestartSec=5",
            'Environment="PYTHONUNBUFFERED=1"',
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
