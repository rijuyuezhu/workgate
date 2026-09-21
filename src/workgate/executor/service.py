"""Local executor service-manager integration.

This module is deliberately host-local.  It manages only the operating-system
launcher for the existing ``workgate executor run`` runtime and never owns
pairing or executor credentials.
"""

from __future__ import annotations

import json
import os
import platform
import plistlib
import shutil
import subprocess
import sys
from dataclasses import dataclass, fields
from enum import StrEnum
from pathlib import Path
from typing import Any

from ..app_paths import ensure_private_directory
from ..config.settings import Settings
from ..persistence import FileStateStore
from ..utils.private_files import (
    atomic_write_private_bytes,
    atomic_write_private_text,
)
from ..version import __version__
from .config import ExecutorConfig
from .profile import ExecutorProfileStore

_SERVICE_NAME = "workgate-executor"
_LAUNCHD_LABEL = "com.workgate.executor"
_WINDOWS_TASK_NAME = "Workgate Executor"
_MAX_COMMAND_OUTPUT = 32_768
_MAX_LOG_READ_BYTES = 256 * 1024
_DEFAULT_LOG_LINES = 100
_MAX_LOG_LINES = 1_000


class ExecutorServiceState(StrEnum):
    """Portable lifecycle state exposed by all supported service managers."""

    NOT_INSTALLED = "not-installed"
    INSTALLED = "installed"
    RUNNING = "running"
    STOPPED = "stopped"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ExecutorServiceStatus:
    """Typed local status independent of service-manager output formats."""

    backend: str
    state: ExecutorServiceState
    installed: bool
    running: bool
    detail: str = ""
    service_file: str | None = None
    log_path: str | None = None
    runtime_current: bool = True


@dataclass(frozen=True, slots=True)
class ExecutorServiceInstallation:
    """Result of installing or refreshing the local executor service."""

    backend: str
    service_file: str
    started: bool
    status: ExecutorServiceStatus


class UnsupportedExecutorServiceError(RuntimeError):
    """Raised when this host has no supported per-user service manager."""


def _json_value(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_value(child) for key, child in value.items()}
    return value


def _executor_service_settings(settings: Settings) -> dict[str, Any]:
    """Snapshot only settings consumed by ExecutorConfig.

    In particular, control-plane auth settings and executor bearer credentials
    are never copied into the managed-service configuration.
    """

    settings_fields = Settings.model_fields
    names = {
        field.name
        for field in fields(ExecutorConfig)
        if field.name in settings_fields
    }
    return {
        name: _json_value(getattr(settings, name)) for name in sorted(names)
    }


def _powershell_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _systemd_quote(value: str) -> str:
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("%", "%%")
        .replace("$", "$$")
    )
    return f'"{escaped}"'


class ExecutorServiceManager:
    """Manage one paired executor through the host's per-user service manager."""

    def __init__(
        self,
        settings: Settings,
        *,
        home: Path | None = None,
        environ: dict[str, str] | None = None,
        system: str | None = None,
        executable: Path | None = None,
    ) -> None:
        self.settings = settings
        self.home = (Path.home() if home is None else Path(home)).expanduser()
        self.environ = dict(os.environ if environ is None else environ)
        self.system = platform.system() if system is None else system
        self.executable = Path(
            sys.executable if executable is None else executable
        )
        self.state_store = FileStateStore(lambda: self.settings.state_dir)

    @property
    def service_dir(self) -> Path:
        return self.settings.state_dir / "executor" / "service"

    @property
    def config_path(self) -> Path:
        return self.service_dir / "config.json"

    @property
    def launcher_path(self) -> Path:
        return self.service_dir / "launcher.py"

    @property
    def metadata_path(self) -> Path:
        return self.service_dir / "installation.json"

    @property
    def log_path(self) -> Path:
        return self.service_dir / "executor.log"

    @property
    def systemd_unit_path(self) -> Path:
        configured = self.environ.get("XDG_CONFIG_HOME")
        base = (
            Path(configured)
            if configured and Path(configured).is_absolute()
            else self.home / ".config"
        )
        return base / "systemd" / "user" / f"{_SERVICE_NAME}.service"

    @property
    def launchd_plist_path(self) -> Path:
        return (
            self.home / "Library" / "LaunchAgents" / f"{_LAUNCHD_LABEL}.plist"
        )

    def _run(
        self,
        command: list[str],
        *,
        check: bool = True,
        timeout: float = 15.0,
    ) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if check and result.returncode:
            detail = (result.stderr or result.stdout).strip()
            if len(detail) > _MAX_COMMAND_OUTPUT:
                detail = detail[-_MAX_COMMAND_OUTPUT:]
            raise RuntimeError(
                f"service command failed ({result.returncode}): {detail or command[0]}"
            )
        return result

    def _powershell_executable(self) -> str | None:
        return (
            shutil.which("powershell.exe")
            or shutil.which("pwsh.exe")
            or shutil.which("powershell")
            or shutil.which("pwsh")
        )

    def _run_powershell(
        self, script: str, *, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        executable = self._powershell_executable()
        if executable is None:
            raise UnsupportedExecutorServiceError(
                "PowerShell is required for the Windows executor service"
            )
        return self._run(
            [
                executable,
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                script,
            ],
            check=check,
        )

    def backend(self, *, require_available: bool = False) -> str:
        if self.system == "Linux":
            if require_available:
                if shutil.which("systemctl") is None:
                    raise UnsupportedExecutorServiceError(
                        "systemd --user is required on Linux"
                    )
                probe = self._run(
                    ["systemctl", "--user", "show-environment"], check=False
                )
                if probe.returncode:
                    raise UnsupportedExecutorServiceError(
                        "systemd user manager is unavailable; log in with a user "
                        "session or enable the user manager before installing"
                    )
            return "systemd"
        if self.system == "Darwin":
            if require_available and shutil.which("launchctl") is None:
                raise UnsupportedExecutorServiceError(
                    "launchctl is required on macOS"
                )
            return "launchd"
        if self.system == "Windows":
            if require_available and self._powershell_executable() is None:
                raise UnsupportedExecutorServiceError(
                    "PowerShell is required on Windows"
                )
            return "scheduled-task"
        raise UnsupportedExecutorServiceError(
            f"managed executor services are unsupported on {self.system}"
        )

    def _ensure_paired(self) -> None:
        if ExecutorProfileStore(self.state_store).load() is None:
            raise RuntimeError(
                "executor is not paired; run "
                "`workgate executor connect <control-url>` before installing the service"
            )

    def _write_service_config(self) -> None:
        ensure_private_directory(self.service_dir)
        payload = json.dumps(
            _executor_service_settings(self.settings),
            indent=2,
            sort_keys=True,
        )
        atomic_write_private_text(self.config_path, payload + "\n")

    def _launcher_source(self) -> str:
        config_path = str(self.config_path.resolve())
        log_path = str(self.log_path.resolve())
        return (
            '"""Private Workgate managed-executor launcher. Generated; do not edit."""\n'
            "from __future__ import annotations\n\n"
            "import contextlib\n"
            "import os\n"
            "import sys\n"
            "import traceback\n"
            "from pathlib import Path\n\n"
            f"CONFIG_PATH = {config_path!r}\n"
            f"LOG_PATH = {log_path!r}\n\n"
            "for _name in list(os.environ):\n"
            "    if _name.startswith('WORKGATE_'):\n"
            "        os.environ.pop(_name, None)\n"
            "os.environ['PYTHONUNBUFFERED'] = '1'\n\n"
            "def _run() -> None:\n"
            "    from workgate.main import main\n"
            "    main(['executor', 'run', '--managed-service', '--config', CONFIG_PATH])\n\n"
            "if os.name == 'nt':\n"
            "    Path(LOG_PATH).parent.mkdir(parents=True, exist_ok=True)\n"
            "    with open(LOG_PATH, 'a', encoding='utf-8', buffering=1) as _log:\n"
            "        with contextlib.redirect_stdout(_log), contextlib.redirect_stderr(_log):\n"
            "            try:\n"
            "                _run()\n"
            "            except BaseException:\n"
            "                traceback.print_exc()\n"
            "                raise\n"
            "else:\n"
            "    _run()\n"
        )

    def _write_launcher(self) -> None:
        atomic_write_private_text(self.launcher_path, self._launcher_source())

    def _runtime_command(self) -> list[str]:
        if getattr(sys, "frozen", False):
            return [
                str(self.executable.resolve()),
                "executor",
                "run",
                "--managed-service",
                "--config",
                str(self.config_path.resolve()),
            ]
        executable = self.executable.resolve()
        if self.system == "Windows":
            pythonw = executable.with_name("pythonw.exe")
            if pythonw.is_file():
                executable = pythonw
        return [str(executable), str(self.launcher_path.resolve())]

    def _write_metadata(self, backend: str, command: list[str]) -> None:
        payload = {
            "version": 1,
            "backend": backend,
            "workgate_version": __version__,
            "command": command,
        }
        atomic_write_private_text(
            self.metadata_path,
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
        )

    def _runtime_current(self) -> bool:
        try:
            payload = json.loads(self.metadata_path.read_text(encoding="utf-8"))
        except OSError, ValueError:
            return False
        command = payload.get("command")
        if not isinstance(command, list) or not all(
            isinstance(item, str) for item in command
        ):
            return False
        return command == self._runtime_command()

    def _write_systemd_unit(self, command: list[str]) -> Path:
        content = "\n".join(
            [
                "[Unit]",
                "Description=Workgate executor",
                "After=network-online.target",
                "Wants=network-online.target",
                "",
                "[Service]",
                "Type=simple",
                "ExecStart="
                + " ".join(_systemd_quote(item) for item in command),
                "Restart=always",
                "RestartSec=5",
                "Environment=PYTHONUNBUFFERED=1",
                "",
                "[Install]",
                "WantedBy=default.target",
                "",
            ]
        )
        self.systemd_unit_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_private_text(self.systemd_unit_path, content)
        return self.systemd_unit_path

    def _write_launchd_plist(self, command: list[str]) -> Path:
        payload = {
            "Label": _LAUNCHD_LABEL,
            "ProgramArguments": command,
            "RunAtLoad": True,
            "KeepAlive": True,
            "ThrottleInterval": 5,
            "StandardOutPath": str(self.log_path.resolve()),
            "StandardErrorPath": str(self.log_path.resolve()),
        }
        self.launchd_plist_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_private_bytes(
            self.launchd_plist_path,
            plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=False),
        )
        return self.launchd_plist_path

    def _windows_registration_script(self, command: list[str]) -> str:
        executable, *arguments = command
        argument_text = subprocess.list2cmdline(arguments)
        return "\n".join(
            [
                "$ErrorActionPreference = 'Stop'",
                "$user = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name",
                (
                    "$action = New-ScheduledTaskAction "
                    f"-Execute {_powershell_literal(executable)} "
                    f"-Argument {_powershell_literal(argument_text)} "
                    f"-WorkingDirectory {_powershell_literal(str(self.service_dir.resolve()))}"
                ),
                "$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user",
                (
                    "$settings = New-ScheduledTaskSettingsSet "
                    "-AllowStartIfOnBatteries -DontStopIfGoingOnBatteries "
                    "-ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew "
                    "-RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) "
                    "-StartWhenAvailable"
                ),
                (
                    "$principal = New-ScheduledTaskPrincipal -UserId $user "
                    "-LogonType Interactive -RunLevel Limited"
                ),
                (
                    "Register-ScheduledTask "
                    f"-TaskName {_powershell_literal(_WINDOWS_TASK_NAME)} "
                    "-Action $action -Trigger $trigger -Settings $settings "
                    "-Principal $principal "
                    f"-Description {_powershell_literal('Workgate executor')} "
                    "-Force | Out-Null"
                ),
            ]
        )

    def _windows_task_status(self) -> dict[str, Any] | None:
        script = "\n".join(
            [
                "$ErrorActionPreference = 'Stop'",
                (
                    "$task = Get-ScheduledTask "
                    f"-TaskName {_powershell_literal(_WINDOWS_TASK_NAME)} "
                    "-ErrorAction SilentlyContinue"
                ),
                "if ($null -eq $task) { exit 3 }",
                (
                    "$info = Get-ScheduledTaskInfo "
                    f"-TaskName {_powershell_literal(_WINDOWS_TASK_NAME)}"
                ),
                (
                    "[Console]::Out.Write(([pscustomobject]@{"
                    "state=$task.State.ToString(); "
                    "last_result=[int64]$info.LastTaskResult"
                    "} | ConvertTo-Json -Compress))"
                ),
            ]
        )
        result = self._run_powershell(script, check=False)
        if result.returncode == 3:
            return None
        if result.returncode:
            detail = (result.stderr or result.stdout).strip()
            raise RuntimeError(f"failed to query executor task: {detail}")
        try:
            payload = json.loads(result.stdout)
        except ValueError as exc:
            raise RuntimeError(
                "invalid executor task status returned by PowerShell"
            ) from exc
        if not isinstance(payload, dict):
            raise RuntimeError(
                "invalid executor task status returned by PowerShell"
            )
        return payload

    def _start_windows_task(self) -> None:
        self._run_powershell(
            f"Start-ScheduledTask -TaskName {_powershell_literal(_WINDOWS_TASK_NAME)}"
        )

    def _stop_windows_task(self) -> None:
        self._run_powershell(
            f"Stop-ScheduledTask -TaskName {_powershell_literal(_WINDOWS_TASK_NAME)}"
        )

    def _unregister_windows_task(self) -> None:
        self._run_powershell(
            f"Unregister-ScheduledTask -TaskName {_powershell_literal(_WINDOWS_TASK_NAME)} "
            "-Confirm:$false"
        )

    def install(self) -> ExecutorServiceInstallation:
        """Install or refresh the native per-user service and start it."""

        self._ensure_paired()
        backend = self.backend(require_available=True)
        self._write_service_config()
        self._write_launcher()
        command = self._runtime_command()
        self._write_metadata(backend, command)

        if backend == "systemd":
            service_file = self._write_systemd_unit(command)
            self._run(["systemctl", "--user", "daemon-reload"])
            self._run(
                ["systemctl", "--user", "enable", f"{_SERVICE_NAME}.service"]
            )
            self._run(
                ["systemctl", "--user", "restart", f"{_SERVICE_NAME}.service"]
            )
        elif backend == "launchd":
            service_file = self._write_launchd_plist(command)
            domain = f"gui/{os.getuid()}"
            self._run(
                ["launchctl", "bootout", domain, str(service_file)],
                check=False,
            )
            self._run(["launchctl", "bootstrap", domain, str(service_file)])
        else:
            service_file = self.metadata_path
            if self._windows_task_status() is not None:
                self._stop_windows_task()
            self._run_powershell(self._windows_registration_script(command))
            self._start_windows_task()

        return ExecutorServiceInstallation(
            backend=backend,
            service_file=str(service_file),
            started=True,
            status=self.status(),
        )

    def uninstall(self) -> ExecutorServiceStatus:
        """Remove only managed-service artifacts; preserve executor identity/profile."""

        backend = self.backend(require_available=True)
        if backend == "systemd":
            if self.systemd_unit_path.exists():
                self._run(
                    [
                        "systemctl",
                        "--user",
                        "disable",
                        "--now",
                        f"{_SERVICE_NAME}.service",
                    ]
                )
                self.systemd_unit_path.unlink(missing_ok=True)
                self._run(["systemctl", "--user", "daemon-reload"])
        elif backend == "launchd":
            if self.launchd_plist_path.exists():
                domain = f"gui/{os.getuid()}"
                loaded = self._run(
                    ["launchctl", "print", f"{domain}/{_LAUNCHD_LABEL}"],
                    check=False,
                )
                if loaded.returncode == 0:
                    self._run(
                        [
                            "launchctl",
                            "bootout",
                            domain,
                            str(self.launchd_plist_path),
                        ]
                    )
                self.launchd_plist_path.unlink(missing_ok=True)
        else:
            if self._windows_task_status() is not None:
                self._stop_windows_task()
                self._unregister_windows_task()
                if self._windows_task_status() is not None:
                    raise RuntimeError(
                        "failed to remove executor scheduled task"
                    )

        for path in (
            self.config_path,
            self.launcher_path,
            self.metadata_path,
            self.log_path,
        ):
            path.unlink(missing_ok=True)
        return self.status()

    def start(self) -> ExecutorServiceStatus:
        backend = self.backend(require_available=True)
        self._require_installed()
        if backend == "systemd":
            self._run(
                ["systemctl", "--user", "start", f"{_SERVICE_NAME}.service"]
            )
        elif backend == "launchd":
            domain = f"gui/{os.getuid()}"
            label = f"{domain}/{_LAUNCHD_LABEL}"
            kickstart = self._run(
                ["launchctl", "kickstart", label],
                check=False,
            )
            if kickstart.returncode:
                self._run(
                    [
                        "launchctl",
                        "bootstrap",
                        domain,
                        str(self.launchd_plist_path),
                    ]
                )
        else:
            self._start_windows_task()
        return self.status()

    def stop(self) -> ExecutorServiceStatus:
        backend = self.backend(require_available=True)
        self._require_installed()
        if backend == "systemd":
            self._run(
                ["systemctl", "--user", "stop", f"{_SERVICE_NAME}.service"],
                check=False,
            )
        elif backend == "launchd":
            self._run(
                [
                    "launchctl",
                    "bootout",
                    f"gui/{os.getuid()}",
                    str(self.launchd_plist_path),
                ],
                check=False,
            )
        else:
            self._stop_windows_task()
        return self.status()

    def restart(self) -> ExecutorServiceStatus:
        backend = self.backend(require_available=True)
        self._require_installed()
        if backend == "systemd":
            self._run(
                ["systemctl", "--user", "restart", f"{_SERVICE_NAME}.service"]
            )
        elif backend == "launchd":
            domain = f"gui/{os.getuid()}"
            self._run(
                ["launchctl", "bootout", domain, str(self.launchd_plist_path)],
                check=False,
            )
            self._run(
                ["launchctl", "bootstrap", domain, str(self.launchd_plist_path)]
            )
        else:
            self._stop_windows_task()
            self._start_windows_task()
        return self.status()

    def _service_file(self, backend: str) -> Path:
        if backend == "systemd":
            return self.systemd_unit_path
        if backend == "launchd":
            return self.launchd_plist_path
        return self.metadata_path

    def _require_installed(self) -> None:
        status = self.status()
        if not status.installed:
            raise RuntimeError(
                "executor service is not installed; run "
                "`workgate executor install-service` first"
            )

    def status(self) -> ExecutorServiceStatus:
        backend = self.backend()
        service_file = self._service_file(backend)
        installed = service_file.exists()
        common = {
            "backend": backend,
            "installed": installed,
            "service_file": str(service_file),
            "log_path": str(self.log_path),
            "runtime_current": self._runtime_current() if installed else True,
        }
        if not installed:
            return ExecutorServiceStatus(
                state=ExecutorServiceState.NOT_INSTALLED,
                running=False,
                **common,
            )

        if backend == "systemd":
            if shutil.which("systemctl") is None:
                return ExecutorServiceStatus(
                    state=ExecutorServiceState.FAILED,
                    running=False,
                    detail="systemctl is unavailable",
                    **common,
                )
            result = self._run(
                [
                    "systemctl",
                    "--user",
                    "is-active",
                    f"{_SERVICE_NAME}.service",
                ],
                check=False,
            )
            active = result.stdout.strip()
            error_detail = result.stderr.strip()
            if result.returncode == 0 and active == "active":
                state = ExecutorServiceState.RUNNING
                running = True
            elif active == "failed" or (
                result.returncode != 0 and not active and error_detail
            ):
                state = ExecutorServiceState.FAILED
                running = False
            elif active in {"inactive", "deactivating", ""}:
                state = ExecutorServiceState.STOPPED
                running = False
            else:
                state = ExecutorServiceState.INSTALLED
                running = False
            detail = active or error_detail
            return ExecutorServiceStatus(
                state=state,
                running=running,
                detail=detail,
                **common,
            )

        if backend == "launchd":
            if shutil.which("launchctl") is None:
                return ExecutorServiceStatus(
                    state=ExecutorServiceState.FAILED,
                    running=False,
                    detail="launchctl is unavailable",
                    **common,
                )
            result = self._run(
                ["launchctl", "print", f"gui/{os.getuid()}/{_LAUNCHD_LABEL}"],
                check=False,
            )
            if result.returncode:
                return ExecutorServiceStatus(
                    state=ExecutorServiceState.STOPPED,
                    running=False,
                    detail=(result.stderr or result.stdout).strip(),
                    **common,
                )
            fields = {}
            for line in result.stdout.splitlines():
                key, separator, value = line.strip().partition("=")
                if separator:
                    fields[key.strip().lower()] = value.strip()
            native_state = fields.get("state", "").lower()
            last_exit_raw = fields.get("last exit code", "")
            try:
                last_exit = int(last_exit_raw)
            except ValueError:
                last_exit = None
            if native_state == "running":
                state = ExecutorServiceState.RUNNING
                running = True
            elif last_exit not in {None, 0}:
                state = ExecutorServiceState.FAILED
                running = False
            else:
                state = ExecutorServiceState.STOPPED
                running = False
            detail_parts = [
                part
                for part in (
                    native_state,
                    f"last exit={last_exit}" if last_exit is not None else "",
                )
                if part
            ]
            return ExecutorServiceStatus(
                state=state,
                running=running,
                detail="; ".join(detail_parts) or "loaded",
                **common,
            )

        try:
            task = self._windows_task_status()
        except (RuntimeError, UnsupportedExecutorServiceError) as exc:
            return ExecutorServiceStatus(
                state=ExecutorServiceState.FAILED,
                running=False,
                detail=str(exc),
                **common,
            )
        if task is None:
            return ExecutorServiceStatus(
                backend=backend,
                state=ExecutorServiceState.FAILED,
                installed=False,
                running=False,
                detail="service metadata exists but scheduled task is missing",
                service_file=str(service_file),
                log_path=str(self.log_path),
                runtime_current=common["runtime_current"],
            )
        task_state = str(task.get("state", ""))
        last_result = task.get("last_result")
        if task_state.lower() == "running":
            state = ExecutorServiceState.RUNNING
            running = True
        elif isinstance(last_result, int) and last_result not in {0, 267009}:
            state = ExecutorServiceState.FAILED
            running = False
        else:
            state = ExecutorServiceState.STOPPED
            running = False
        return ExecutorServiceStatus(
            state=state,
            running=running,
            detail=f"{task_state}; last result={last_result}",
            **common,
        )

    def logs(self, *, lines: int = _DEFAULT_LOG_LINES) -> str:
        """Return a bounded recent log view from the native backend."""

        if not 1 <= lines <= _MAX_LOG_LINES:
            raise ValueError(f"lines must be between 1 and {_MAX_LOG_LINES}")
        backend = self.backend()
        if backend == "systemd" and self.systemd_unit_path.exists():
            result = self._run(
                [
                    "journalctl",
                    "--user",
                    "-u",
                    f"{_SERVICE_NAME}.service",
                    "-n",
                    str(lines),
                    "--no-pager",
                    "--output=cat",
                ],
                check=False,
            )
            output = result.stdout if result.returncode == 0 else result.stderr
            return output[-_MAX_LOG_READ_BYTES:]

        try:
            with self.log_path.open("rb") as handle:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                handle.seek(max(0, size - _MAX_LOG_READ_BYTES))
                data = handle.read(_MAX_LOG_READ_BYTES)
        except FileNotFoundError:
            return ""
        text = data.decode("utf-8", errors="replace")
        return "\n".join(text.splitlines()[-lines:])


__all__ = [
    "ExecutorServiceInstallation",
    "ExecutorServiceManager",
    "ExecutorServiceState",
    "ExecutorServiceStatus",
    "UnsupportedExecutorServiceError",
]
