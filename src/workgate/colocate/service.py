"""Install the ordinary colocate launcher as a native per-user service."""

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

from ..app_paths import app_paths, ensure_private_directory
from ..config.executor import EXECUTOR_SETTING_NAMES
from ..config.settings import load_settings
from ..executor.profile import ExecutorProfileStore
from ..persistence import FileStateStore
from ..platform.process import noninteractive_creationflags
from ..platform.windows_task import WindowsUserTask
from ..utils.private_files import atomic_write_private_text
from ..utils.systemd import systemd_quote, systemd_user_unit_path

_SERVICE_NAME = "workgate-colocate.service"
_MANAGED_HEADER = "# Managed by workgate colocate install-service"
_WINDOWS_TASK_NAME = "Workgate Colocate"
_WINDOWS_TASK_DESCRIPTION = "Managed by Workgate: colocate"


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


def _windows_task() -> WindowsUserTask:
    def run(command: list[str]) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=20,
            creationflags=noninteractive_creationflags(),
        )
        if result.returncode:
            raise RuntimeError(
                f"Task Scheduler command failed: {result.stderr or result.stdout}"
            )
        return result

    return WindowsUserTask(
        _WINDOWS_TASK_NAME,
        description=_WINDOWS_TASK_DESCRIPTION,
        run=run,
    )


def _load_state() -> str:
    return _systemctl(
        "show", _SERVICE_NAME, "--property=LoadState", "--value"
    ).stdout.strip()


def _windows_service_dir() -> Path:
    return app_paths().state_dir / "colocate" / "service"


def _config_file(value: str | None) -> Path | None:
    if value is None:
        return None
    path = Path(value).expanduser().absolute()
    if not path.is_file():
        raise FileNotFoundError(path)
    return path


def install_colocate_service(args: argparse.Namespace) -> None:
    """Create and start one systemd user unit, without introducing runtime state."""
    windows = sys.platform == "win32"
    if windows:
        if not WindowsUserTask.available():
            raise RuntimeError(
                "colocate install-service requires Windows Task Scheduler"
            )
        _windows_task().status()  # Fail closed on another account's task.
    elif sys.platform != "linux" or shutil.which("systemctl") is None:
        raise RuntimeError(
            "colocate install-service requires Linux systemd --user"
        )
    if any(name.startswith("WORKGATE_") for name in os.environ):
        raise RuntimeError(
            "colocate install-service requires durable YAML config, "
            "not WORKGATE_* environment overrides"
        )
    if not windows:
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

    if windows and getattr(sys, "frozen", False):
        raise RuntimeError(
            "Windows frozen Colocate requires a windowless executable; "
            "this frozen build is not yet service-capable"
        )
    executable = str(Path(sys.executable).absolute())
    colocate_args = ["colocate"]
    if control_config is not None:
        colocate_args.extend(["--control-config", str(control_config)])
    if executor_config is not None:
        colocate_args.extend(["--executor-config", str(executor_config)])
    command = (
        [executable, *colocate_args]
        if getattr(sys, "frozen", False)
        else [executable, "-m", "workgate.main", *colocate_args]
    )

    if windows:
        pythonw = Path(executable).with_name("pythonw.exe")
        if not pythonw.is_file():
            raise RuntimeError(
                "Windows source Colocate service requires pythonw.exe"
            )
        service_dir = ensure_private_directory(_windows_service_dir())
        launcher = service_dir / "launcher.py"
        log_path = service_dir / "colocate.log"
        source = (
            '"""Private Workgate Colocate service launcher."""\n'
            "import contextlib\nimport os\nimport traceback\n"
            "for key in list(os.environ):\n"
            "    if key.startswith('WORKGATE_'):\n"
            "        os.environ.pop(key, None)\n"
            "os.environ['PYTHONUNBUFFERED'] = '1'\n"
            f"with open({str(log_path)!r}, 'a', encoding='utf-8', buffering=1) as log:\n"
            "    with contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):\n"
            "        try:\n"
            "            from workgate.main import main\n"
            f"            main({colocate_args!r})\n"
            "        except BaseException:\n"
            "            traceback.print_exc()\n"
            "            raise\n"
        )
        atomic_write_private_text(launcher, source)
        task = _windows_task()
        task.install([str(pythonw), str(launcher)], cwd=str(service_dir))
        task.start()
        print(f"Installed and started {_WINDOWS_TASK_NAME} (Task Scheduler)")
        return

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
    if not unit.exists() and _load_state() != "not-found":
        raise RuntimeError(
            "refusing to replace a loaded Colocate unit from another path"
        )
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


def manage_colocate_service(action: str) -> None:
    """Manage only the installed Colocate service, never its paired identity."""
    if sys.platform == "win32":
        if not WindowsUserTask.available():
            raise RuntimeError("Windows Task Scheduler requires PowerShell")
        task = _windows_task()
        state = task.status()
        if action == "status":
            print(
                f"Colocate service: {state['state'] if state else 'not-installed'}"
            )
            return
        if action == "uninstall":
            task.uninstall()
            directory = _windows_service_dir()
            (directory / "launcher.py").unlink(missing_ok=True)
            (directory / "colocate.log").unlink(missing_ok=True)
            print("Uninstalled Workgate Colocate task")
            return
        if state is None:
            raise RuntimeError("Colocate service is not installed")
        if action in {"stop", "restart"}:
            task.stop()
        if action in {"start", "restart"}:
            task.start()
        print(f"Colocate service: {action}")
        return

    if sys.platform != "linux" or shutil.which("systemctl") is None:
        raise RuntimeError(
            "Colocate service requires Linux systemd or Windows Task Scheduler"
        )
    if action == "status":
        unit = systemd_user_unit_path(
            _systemctl("show", "--property=UnitPath", "--value").stdout,
            _SERVICE_NAME,
        )
        if not unit.exists() and not unit.is_symlink():
            # Don't mistake a loaded unit from another search path for absence.
            load = _load_state()
            print(
                "Colocate service: "
                + ("not-installed" if load == "not-found" else "unknown")
            )
            return
        result = subprocess.run(
            ["systemctl", "--user", "is-active", _SERVICE_NAME],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
        # A unit exists, so even an empty/odd manager response must not
        # permit first-run setup to treat this service as absent.
        native_status = result.stdout.strip()
        if native_status in {"", "not-installed"}:
            native_status = "unknown"
        print(f"Colocate service: {native_status}")
        return
    if action == "uninstall":
        unit = systemd_user_unit_path(
            _systemctl("show", "--property=UnitPath", "--value").stdout,
            _SERVICE_NAME,
        )
        if unit.is_symlink() or (
            unit.is_file() and not unit.read_text().startswith(_MANAGED_HEADER)
        ):
            raise RuntimeError(f"refusing to remove unmanaged service: {unit}")
        if unit.exists():
            _systemctl("disable", "--now", _SERVICE_NAME)
            unit.unlink()
            _systemctl("daemon-reload")
        print("Uninstalled Workgate Colocate service")
        return
    _systemctl(action, _SERVICE_NAME)
    print(f"Colocate service: {action}")
