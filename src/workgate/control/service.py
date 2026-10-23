"""Native per-user lifecycle for the ordinary Control process."""

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

from ..app_paths import app_paths, ensure_private_directory
from ..config.control import CONTROL_SETTING_NAMES
from ..config.settings import load_settings
from ..config.surface import CLI_UNSET
from ..platform.process import noninteractive_creationflags
from ..platform.windows_task import WindowsUserTask
from ..utils.private_files import atomic_write_private_text
from ..utils.systemd import systemd_quote, systemd_user_unit_path

_SERVICE = "workgate-control.service"
_HEADER = "# Managed by workgate control install-service"
_TASK = "Workgate Control"
_TASK_DESCRIPTION = "Managed by Workgate: control"


def _systemctl(*args: str) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["systemctl", "--user", *args],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(
            f"systemctl --user {' '.join(args)} failed: "
            f"{(result.stderr or result.stdout).strip() or result.returncode}"
        )
    return result


def _task() -> WindowsUserTask:
    def run(command: list[str]) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
            creationflags=noninteractive_creationflags(),
        )
        if result.returncode:
            raise RuntimeError(
                f"Task Scheduler command failed: {result.stderr or result.stdout}"
            )
        return result

    return WindowsUserTask(_TASK, description=_TASK_DESCRIPTION, run=run)


def _unit() -> Path:
    return systemd_user_unit_path(
        _systemctl("show", "--property=UnitPath", "--value").stdout, _SERVICE
    )


def _owned_unit() -> Path:
    path = _unit()
    if path.is_symlink() or (
        path.exists() and not path.read_text().startswith(_HEADER)
    ):
        raise RuntimeError(f"refusing to manage an unmanaged service: {path}")
    return path


def _config(value: str | None) -> Path:
    path = (
        Path(value).expanduser().absolute()
        if value
        else app_paths().config_file
    )
    if not path.is_file():
        raise RuntimeError(
            f"Control service requires a durable YAML config: {path}"
        )
    return path


def install_control_service(args: argparse.Namespace) -> None:
    """Install and start Control without changing settings or its state identity."""
    if any(
        getattr(args, field, CLI_UNSET) is not CLI_UNSET
        for field in CONTROL_SETTING_NAMES
    ):
        raise RuntimeError(
            "control install-service accepts --config, but not transient "
            "Control CLI setting overrides; save them in YAML"
        )
    if any(key.startswith("WORKGATE_") for key in os.environ):
        raise RuntimeError(
            "control install-service requires durable YAML configuration, "
            "not WORKGATE_* environment overrides"
        )
    config = _config(args.config)
    settings = load_settings(config, setting_names=CONTROL_SETTING_NAMES)
    if settings.mode != "http":
        raise RuntimeError("Control service requires mode: http for the WebUI")
    executable = Path(sys.executable).absolute()
    if getattr(sys, "frozen", False):
        raise RuntimeError(
            "Control service requires a Python source installation"
        )
    command = [
        str(executable),
        "-m",
        "workgate.main",
        "control",
        "--config",
        str(config),
    ]
    if sys.platform == "win32":
        if not WindowsUserTask.available():
            raise RuntimeError(
                "Control service requires Windows Task Scheduler"
            )
        task = _task()
        task.status()  # Reject a task owned by someone else before writing files.
        pythonw = executable.with_name("pythonw.exe")
        if not pythonw.is_file():
            raise RuntimeError("Windows Control service requires pythonw.exe")
        directory = ensure_private_directory(
            app_paths().state_dir / "control" / "service"
        )
        launcher = directory / "launcher.py"
        log = directory / "control.log"
        source = (
            '"""Private Workgate Control service launcher."""\n'
            "import contextlib\nimport os\nimport traceback\n"
            "for key in list(os.environ):\n"
            "    if key.startswith('WORKGATE_'):\n"
            "        os.environ.pop(key, None)\n"
            "os.environ['PYTHONUNBUFFERED'] = '1'\n"
            f"with open({str(log)!r}, 'a', encoding='utf-8', buffering=1) as stream:\n"
            "    with contextlib.redirect_stdout(stream), contextlib.redirect_stderr(stream):\n"
            "        try:\n"
            "            from workgate.main import main\n"
            f"            main(['control', '--config', {str(config)!r}])\n"
            "        except BaseException:\n"
            "            traceback.print_exc()\n"
            "            raise\n"
        )
        atomic_write_private_text(launcher, source)
        task.install([str(pythonw), str(launcher)], cwd=str(directory))
        task.start()
        print(f"Installed and started {_TASK} (Task Scheduler)")
        return
    if sys.platform != "linux" or not shutil.which("systemctl"):
        raise RuntimeError("Control service requires Linux systemd --user")
    unit = _owned_unit()
    content = "\n".join(
        [
            _HEADER,
            "[Unit]",
            "Description=Workgate Control",
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
            # User systemd managers inherit imported environment variables.
            # A service pinned to YAML must not be silently overridden by them.
            "UnsetEnvironment="
            + " ".join(
                sorted(
                    {
                        "WORKGATE_CONFIG",
                        *(
                            f"WORKGATE_{field.upper()}"
                            for field in CONTROL_SETTING_NAMES
                        ),
                    }
                )
            ),
            "",
            "[Install]",
            "WantedBy=default.target",
            "",
        ]
    )
    atomic_write_private_text(unit, content)
    _systemctl("daemon-reload")
    _systemctl("enable", _SERVICE)
    _systemctl("restart", _SERVICE)
    print(f"Installed and started {_SERVICE} ({unit})")


def manage_control_service(action: str) -> None:
    """Only operate on our own service, never unrelated services or Control data."""
    if sys.platform == "win32":
        if not WindowsUserTask.available():
            raise RuntimeError(
                "Control service requires Windows Task Scheduler"
            )
        task = _task()
        status = task.status()
        if action == "status":
            print(
                f"Control service: {status['state'] if status else 'not-installed'}"
            )
            return
        if action == "uninstall":
            if status is not None:
                task.uninstall()
                directory = app_paths().state_dir / "control" / "service"
                (directory / "launcher.py").unlink(missing_ok=True)
            # Retain the log, state, and files if no managed task existed.
            print("Uninstalled Workgate Control task")
            return
        if status is None:
            raise RuntimeError("Control service is not installed")
        if action in {"stop", "restart"}:
            task.stop()
        if action in {"start", "restart"}:
            task.start()
        print(f"Control service: {action}")
        return
    if sys.platform != "linux" or not shutil.which("systemctl"):
        raise RuntimeError("Control service requires Linux systemd --user")
    if action == "status":
        if not _owned_unit().is_file():
            print("Control service: not-installed")
            return
        result = subprocess.run(
            ["systemctl", "--user", "is-active", _SERVICE],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        print(f"Control service: {result.stdout.strip() or 'unknown'}")
        return
    if action == "uninstall":
        unit = _owned_unit()
        if unit.exists():
            _systemctl("disable", "--now", _SERVICE)
            unit.unlink()
            _systemctl("daemon-reload")
        print("Uninstalled Workgate Control service")
        return
    unit = _owned_unit()
    if not unit.is_file():
        raise RuntimeError("Control service is not installed")
    _systemctl(action, _SERVICE)
    print(f"Control service: {action}")
