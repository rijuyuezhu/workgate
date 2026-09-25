"""Local executor service-manager integration.

This module is deliberately host-local.  It manages only the operating-system
launcher for the existing ``workgate executor run`` runtime and never owns
pairing or executor credentials.
"""

from __future__ import annotations

import json
import ntpath
import os
import platform
import plistlib
import posixpath
import shlex
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
_WINDOWS_TASK_PATH = "\\"
_MAX_COMMAND_OUTPUT = 32_768
_MAX_LOG_READ_BYTES = 256 * 1024
_DEFAULT_LOG_LINES = 100
_MAX_LOG_LINES = 1_000
_POSIX_DEFAULT_PATH_DIRS = (
    "/usr/local/bin",
    "/usr/local/sbin",
    "/usr/bin",
    "/bin",
    "/usr/sbin",
    "/sbin",
)
_LAUNCHD_DEFAULT_PATH_DIRS = (
    "/opt/homebrew/bin",
    "/opt/homebrew/sbin",
    *_POSIX_DEFAULT_PATH_DIRS,
)
_WINDOWS_SCHEDULER_SUCCESS_RESULTS = frozenset(
    {
        0,
        *range(0x00041300, 0x00041309),
        0x0004131B,
        0x0004131C,
        0x00041325,
    }
)


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


def _systemd_quote(value: str, *, escape_dollar: bool = True) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    if escape_dollar:
        escaped = escaped.replace("$", "$$")
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
    def windows_frozen_launcher_path(self) -> Path:
        return self.service_dir / "launcher.ps1"

    @property
    def metadata_path(self) -> Path:
        return self.service_dir / "installation.json"

    @property
    def log_path(self) -> Path:
        return self.service_dir / "executor.log"

    @property
    def systemd_unit_path(self) -> Path:
        stored = self._metadata_service_file("systemd")
        if stored is not None:
            return stored
        return (
            self.home
            / ".config"
            / "systemd"
            / "user"
            / f"{_SERVICE_NAME}.service"
        )

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
        windows_path = self._managed_windows_path()
        return (
            '"""Private Workgate managed-executor launcher. Generated; do not edit."""\n'
            "from __future__ import annotations\n\n"
            "import contextlib\n"
            "import os\n"
            "import sys\n"
            "import traceback\n"
            "from pathlib import Path\n\n"
            f"CONFIG_PATH = {config_path!r}\n"
            f"LOG_PATH = {log_path!r}\n"
            f"WINDOWS_PATH = {windows_path!r}\n\n"
            "for _name in list(os.environ):\n"
            "    if _name.startswith('WORKGATE_'):\n"
            "        os.environ.pop(_name, None)\n"
            "os.environ['PYTHONUNBUFFERED'] = '1'\n"
            "if os.name == 'nt' and WINDOWS_PATH:\n"
            "    _existing_path = os.environ.get('PATH', '')\n"
            "    os.environ['PATH'] = WINDOWS_PATH + (os.pathsep + _existing_path if _existing_path else '')\n\n"
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

    def _windows_frozen_launcher_source(self, command: list[str]) -> str:
        invocation = " ".join(_powershell_literal(item) for item in command)
        log_path = _powershell_literal(str(self.log_path.resolve()))
        managed_path = self._managed_windows_path()
        managed_path_literal = _powershell_literal(managed_path)
        return "\n".join(
            [
                "$ErrorActionPreference = 'Stop'",
                f"$logPath = {log_path}",
                f"$managedPath = {managed_path_literal}",
                (
                    "if ($managedPath) { "
                    "$env:PATH = if ($env:PATH) { "
                    "$managedPath + ';' + $env:PATH "
                    "} else { $managedPath } }"
                ),
                "$utf8 = New-Object System.Text.UTF8Encoding($false)",
                "function Write-WorkgateLog([string]$line) {",
                "    [System.IO.File]::AppendAllText(",
                "        $logPath,",
                "        $line + [Environment]::NewLine,",
                "        $utf8",
                "    )",
                "}",
                "try {",
                f"    & {invocation} 2>&1 | ForEach-Object {{",
                "        Write-WorkgateLog ([string]$_)",
                "    }",
                "    $exitCode = $LASTEXITCODE",
                "    if ($null -eq $exitCode) { $exitCode = 1 }",
                "} catch {",
                "    Write-WorkgateLog ([string]$_)",
                "    $exitCode = 1",
                "}",
                "exit $exitCode",
                "",
            ]
        )

    def _write_launcher(self, command: list[str] | None = None) -> None:
        frozen = bool(getattr(sys, "frozen", False))
        if command is None:
            command = self._runtime_command()
        if frozen and self.system == "Windows":
            atomic_write_private_text(
                self.windows_frozen_launcher_path,
                self._windows_frozen_launcher_source(command),
            )
            self.launcher_path.unlink(missing_ok=True)
            return
        self.windows_frozen_launcher_path.unlink(missing_ok=True)
        if frozen:
            self.launcher_path.unlink(missing_ok=True)
            return
        atomic_write_private_text(self.launcher_path, self._launcher_source())

    def _runtime_command(self) -> list[str]:
        # Preserve a virtualenv/pipx interpreter symlink. Resolving it can escape
        # the environment that actually contains the Workgate installation.
        executable = self.executable.absolute()
        if getattr(sys, "frozen", False):
            return [
                str(executable),
                "executor",
                "run",
                "--managed-service",
                "--config",
                str(self.config_path.resolve()),
            ]
        if self.system == "Windows":
            pythonw = executable.with_name("pythonw.exe")
            if pythonw.is_file():
                executable = pythonw
        return [str(executable), str(self.launcher_path.resolve())]

    def _windows_service_command(self, runtime_command: list[str]) -> list[str]:
        if not getattr(sys, "frozen", False):
            return runtime_command
        executable = self._powershell_executable()
        if executable is None:
            raise UnsupportedExecutorServiceError(
                "PowerShell is required for the Windows executor service"
            )
        return [
            executable,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(self.windows_frozen_launcher_path.resolve()),
        ]

    def _read_metadata(self) -> dict[str, Any] | None:
        try:
            payload = json.loads(self.metadata_path.read_text(encoding="utf-8"))
        except OSError, ValueError:
            return None
        return payload if isinstance(payload, dict) else None

    def _write_metadata(
        self,
        backend: str,
        command: list[str],
        *,
        service_file: Path | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "version": 2,
            "backend": backend,
            "workgate_version": __version__,
            "command": command,
        }
        if service_file is not None:
            payload["service_file"] = str(service_file.absolute())
        atomic_write_private_text(
            self.metadata_path,
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
        )

    def _metadata_service_file(self, backend: str) -> Path | None:
        payload = self._read_metadata()
        if payload is None or payload.get("backend") != backend:
            return None
        value = payload.get("service_file")
        if not isinstance(value, str):
            return None
        path = Path(value)
        if not path.is_absolute():
            return None
        if backend == "systemd" and not (
            path.name == f"{_SERVICE_NAME}.service"
            and path.parent.name == "user"
            and path.parent.parent.name == "systemd"
        ):
            return None
        return path

    def _runtime_current(self) -> bool:
        payload = self._read_metadata()
        if payload is None or payload.get("backend") != self.backend():
            return False
        command = payload.get("command")
        if not isinstance(command, list) or not all(
            isinstance(item, str) for item in command
        ):
            return False
        return command == self._runtime_command()

    def _managed_posix_path(self, defaults: tuple[str, ...]) -> str:
        configured_entries = self.environ.get("PATH", "").split(":")
        entries = (
            *(
                entry
                for entry in configured_entries
                if entry and posixpath.isabs(entry)
            ),
            *defaults,
        )
        return ":".join(dict.fromkeys(entries))

    def _managed_windows_path(self) -> str:
        entries: list[str] = []
        for raw_entry in self.environ.get("PATH", "").split(";"):
            entry = raw_entry.strip().strip('"')
            if entry and ntpath.isabs(entry):
                entries.append(entry)
        return ";".join(dict.fromkeys(entries))

    def _systemd_environment_lines(self) -> list[str]:
        values = [
            "PYTHONUNBUFFERED=1",
            f"PATH={self._managed_posix_path(_POSIX_DEFAULT_PATH_DIRS)}",
        ]
        return [
            f"Environment={_systemd_quote(value, escape_dollar=False)}"
            for value in values
        ]

    def _systemd_install_unit_path(self) -> Path:
        result = self._run(
            [
                "systemctl",
                "--user",
                "show",
                "--property=UnitPath",
                "--value",
            ],
            check=False,
        )
        if result.returncode:
            detail = (result.stderr or result.stdout).strip()
            raise RuntimeError(
                f"failed to query systemd user unit path: {detail or result.returncode}"
            )
        try:
            unit_paths = shlex.split(result.stdout.strip())
        except ValueError as exc:
            raise RuntimeError(
                "systemd returned an invalid user unit path"
            ) from exc
        for raw_path in unit_paths:
            path = Path(raw_path)
            if (
                path.is_absolute()
                and path.name == "user"
                and path.parent.name == "systemd"
            ):
                return path / f"{_SERVICE_NAME}.service"
        raise RuntimeError(
            "systemd user manager did not expose a persistent user unit path"
        )

    def _write_systemd_unit(
        self, command: list[str], *, path: Path | None = None
    ) -> Path:
        target = self.systemd_unit_path if path is None else path
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
                *self._systemd_environment_lines(),
                "",
                "[Install]",
                "WantedBy=default.target",
                "",
            ]
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_private_text(target, content)
        return target

    def _launchd_path(self) -> str:
        return self._managed_posix_path(_LAUNCHD_DEFAULT_PATH_DIRS)

    def _write_launchd_plist(self, command: list[str]) -> Path:
        payload = {
            "Label": _LAUNCHD_LABEL,
            "ProgramArguments": command,
            "RunAtLoad": True,
            "KeepAlive": True,
            "ThrottleInterval": 5,
            "EnvironmentVariables": {
                "PATH": self._launchd_path(),
                "PYTHONUNBUFFERED": "1",
            },
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
                    "-RestartCount 255 -RestartInterval (New-TimeSpan -Minutes 1) "
                    "-StartWhenAvailable"
                ),
                (
                    "$principal = New-ScheduledTaskPrincipal -UserId $user "
                    "-LogonType Interactive -RunLevel Limited"
                ),
                (
                    "Register-ScheduledTask "
                    f"-TaskPath {_powershell_literal(_WINDOWS_TASK_PATH)} "
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
                    f"-TaskPath {_powershell_literal(_WINDOWS_TASK_PATH)} "
                    f"-TaskName {_powershell_literal(_WINDOWS_TASK_NAME)} "
                    "-ErrorAction SilentlyContinue"
                ),
                "if ($null -eq $task) { exit 3 }",
                "$info = Get-ScheduledTaskInfo -InputObject $task",
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
            "Start-ScheduledTask "
            f"-TaskPath {_powershell_literal(_WINDOWS_TASK_PATH)} "
            f"-TaskName {_powershell_literal(_WINDOWS_TASK_NAME)}"
        )

    def _stop_windows_task(self) -> None:
        self._run_powershell(
            "Stop-ScheduledTask "
            f"-TaskPath {_powershell_literal(_WINDOWS_TASK_PATH)} "
            f"-TaskName {_powershell_literal(_WINDOWS_TASK_NAME)}"
        )

    def _unregister_windows_task(self) -> None:
        self._run_powershell(
            "Unregister-ScheduledTask "
            f"-TaskPath {_powershell_literal(_WINDOWS_TASK_PATH)} "
            f"-TaskName {_powershell_literal(_WINDOWS_TASK_NAME)} "
            "-Confirm:$false"
        )

    def install(self) -> ExecutorServiceInstallation:
        """Install or refresh the native per-user service and start it."""

        self._ensure_paired()
        backend = self.backend(require_available=True)
        self._write_service_config()
        command = self._runtime_command()
        self._write_launcher(command)

        if backend == "systemd":
            service_file = self._write_systemd_unit(
                command,
                path=self._systemd_install_unit_path(),
            )
            self._run(["systemctl", "--user", "daemon-reload"])
            self._run(
                ["systemctl", "--user", "enable", f"{_SERVICE_NAME}.service"]
            )
            self._write_metadata(
                backend,
                command,
                service_file=service_file,
            )
            self._run(
                ["systemctl", "--user", "restart", f"{_SERVICE_NAME}.service"]
            )
        elif backend == "launchd":
            service_file = self._write_launchd_plist(command)
            domain = f"gui/{os.getuid()}"
            self._run(
                ["launchctl", "bootout", self._launchd_service_target],
                check=False,
            )
            self._run(["launchctl", "bootstrap", domain, str(service_file)])
            self._write_metadata(
                backend,
                command,
                service_file=service_file,
            )
        else:
            service_file = self.metadata_path
            if self._windows_task_status() is not None:
                self._stop_windows_task()
            service_command = self._windows_service_command(command)
            self._run_powershell(
                self._windows_registration_script(service_command)
            )
            self._write_metadata(backend, command)
            self._start_windows_task()

        return ExecutorServiceInstallation(
            backend=backend,
            service_file=str(service_file),
            started=True,
            status=self.status(),
        )

    def _systemd_native_state(self) -> dict[str, str]:
        result = self._run(
            [
                "systemctl",
                "--user",
                "show",
                f"{_SERVICE_NAME}.service",
                "--property=LoadState",
                "--property=ActiveState",
                "--property=SubState",
                "--property=FragmentPath",
            ],
            check=False,
        )
        if result.returncode:
            detail = (result.stderr or result.stdout).strip()
            raise RuntimeError(
                f"failed to query executor systemd service: {detail}"
            )
        values: dict[str, str] = {}
        for line in result.stdout.splitlines():
            key, separator, value = line.partition("=")
            if separator:
                values[key] = value
        return values

    @property
    def _launchd_service_target(self) -> str:
        return f"gui/{os.getuid()}/{_LAUNCHD_LABEL}"

    def uninstall(self) -> ExecutorServiceStatus:
        """Remove only managed-service artifacts; preserve executor identity/profile."""

        backend = self.backend(require_available=True)
        if backend == "systemd":
            unit = f"{_SERVICE_NAME}.service"
            unit_path = self.systemd_unit_path
            definition_present = unit_path.exists()
            native_before = self._systemd_native_state()
            native_loaded = native_before.get("LoadState") != "not-found"
            if not definition_present and native_loaded:
                fragment_raw = native_before.get("FragmentPath", "")
                if fragment_raw:
                    fragment_path = Path(fragment_raw)
                    install_path = self._systemd_install_unit_path()
                    if (
                        fragment_path.is_absolute()
                        and fragment_path.absolute() == install_path.absolute()
                    ):
                        unit_path = fragment_path
                        definition_present = unit_path.exists()
            if definition_present:
                self._run(["systemctl", "--user", "disable", "--now", unit])
            elif native_loaded:
                self._run(["systemctl", "--user", "stop", unit])
                self._run(
                    ["systemctl", "--user", "disable", unit],
                    check=False,
                )
            unit_path.unlink(missing_ok=True)
            self._run(["systemctl", "--user", "daemon-reload"])
            native = self._systemd_native_state()
            if native.get("LoadState") != "not-found":
                raise RuntimeError("failed to unload executor systemd service")
        elif backend == "launchd":
            loaded = self._run(
                ["launchctl", "print", self._launchd_service_target],
                check=False,
            )
            if loaded.returncode == 0:
                self._run(
                    ["launchctl", "bootout", self._launchd_service_target]
                )
            self.launchd_plist_path.unlink(missing_ok=True)
            loaded = self._run(
                ["launchctl", "print", self._launchd_service_target],
                check=False,
            )
            if loaded.returncode == 0:
                raise RuntimeError("failed to unload executor LaunchAgent")
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
            self.windows_frozen_launcher_path,
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
            kickstart = self._run(
                ["launchctl", "kickstart", self._launchd_service_target],
                check=False,
            )
            if kickstart.returncode:
                if not self.launchd_plist_path.exists():
                    raise RuntimeError(
                        "executor LaunchAgent definition is missing; "
                        "run workgate executor install-service to refresh"
                    )
                self._run(
                    [
                        "launchctl",
                        "bootstrap",
                        f"gui/{os.getuid()}",
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
                ["systemctl", "--user", "stop", f"{_SERVICE_NAME}.service"]
            )
        elif backend == "launchd":
            stopped = self._run(
                ["launchctl", "bootout", self._launchd_service_target],
                check=False,
            )
            if stopped.returncode:
                loaded = self._run(
                    ["launchctl", "print", self._launchd_service_target],
                    check=False,
                )
                if loaded.returncode == 0:
                    detail = (stopped.stderr or stopped.stdout).strip()
                    raise RuntimeError(
                        "failed to stop executor LaunchAgent"
                        + (f": {detail}" if detail else "")
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
            kickstart = self._run(
                [
                    "launchctl",
                    "kickstart",
                    "-k",
                    self._launchd_service_target,
                ],
                check=False,
            )
            if kickstart.returncode:
                if not self.launchd_plist_path.exists():
                    raise RuntimeError(
                        "executor LaunchAgent definition is missing; "
                        "run workgate executor install-service to refresh"
                    )
                self._run(
                    [
                        "launchctl",
                        "bootstrap",
                        f"gui/{os.getuid()}",
                        str(self.launchd_plist_path),
                    ]
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

    def _windows_status(self, service_file: Path) -> ExecutorServiceStatus:
        metadata_present = service_file.exists()
        try:
            task = self._windows_task_status()
        except (RuntimeError, UnsupportedExecutorServiceError) as exc:
            return ExecutorServiceStatus(
                backend="scheduled-task",
                state=ExecutorServiceState.FAILED,
                installed=metadata_present,
                running=False,
                detail=str(exc),
                service_file=str(service_file),
                log_path=str(self.log_path),
                runtime_current=(
                    self._runtime_current() if metadata_present else False
                ),
            )

        if task is None:
            if not metadata_present:
                return ExecutorServiceStatus(
                    backend="scheduled-task",
                    state=ExecutorServiceState.NOT_INSTALLED,
                    installed=False,
                    running=False,
                    service_file=str(service_file),
                    log_path=str(self.log_path),
                )
            return ExecutorServiceStatus(
                backend="scheduled-task",
                state=ExecutorServiceState.FAILED,
                installed=False,
                running=False,
                detail="service metadata exists but scheduled task is missing",
                service_file=str(service_file),
                log_path=str(self.log_path),
                runtime_current=False,
            )

        task_state = str(task.get("state", ""))
        normalized_task_state = task_state.lower()
        last_result = task.get("last_result")
        if normalized_task_state == "running":
            state = ExecutorServiceState.RUNNING
            running = True
        elif normalized_task_state == "disabled":
            state = ExecutorServiceState.FAILED
            running = False
        elif normalized_task_state == "queued":
            state = ExecutorServiceState.INSTALLED
            running = False
        elif (
            isinstance(last_result, int)
            and last_result not in _WINDOWS_SCHEDULER_SUCCESS_RESULTS
        ):
            state = ExecutorServiceState.FAILED
            running = False
        else:
            state = ExecutorServiceState.STOPPED
            running = False

        detail_parts = [f"{task_state}; last result={last_result}"]
        if not metadata_present:
            detail_parts.append(
                "service metadata missing; reinstall to refresh"
            )
        return ExecutorServiceStatus(
            backend="scheduled-task",
            state=state,
            installed=True,
            running=running,
            detail="; ".join(detail_parts),
            service_file=str(service_file),
            log_path=str(self.log_path),
            runtime_current=(
                self._runtime_current() if metadata_present else False
            ),
        )

    def _systemd_status(self, service_file: Path) -> ExecutorServiceStatus:
        definition_present = service_file.exists()
        if shutil.which("systemctl") is None:
            if not definition_present:
                return ExecutorServiceStatus(
                    backend="systemd",
                    state=ExecutorServiceState.NOT_INSTALLED,
                    installed=False,
                    running=False,
                    service_file=str(service_file),
                    log_path=str(self.log_path),
                )
            return ExecutorServiceStatus(
                backend="systemd",
                state=ExecutorServiceState.FAILED,
                installed=True,
                running=False,
                detail="systemctl is unavailable",
                service_file=str(service_file),
                log_path=str(self.log_path),
                runtime_current=self._runtime_current(),
            )

        try:
            native = self._systemd_native_state()
        except RuntimeError as exc:
            return ExecutorServiceStatus(
                backend="systemd",
                state=ExecutorServiceState.FAILED,
                installed=definition_present,
                running=False,
                detail=str(exc),
                service_file=str(service_file),
                log_path=str(self.log_path),
                runtime_current=(
                    self._runtime_current() if definition_present else False
                ),
            )

        load_state = native.get("LoadState", "")
        active_state = native.get("ActiveState", "")
        sub_state = native.get("SubState", "")
        fragment_raw = native.get("FragmentPath", "")
        fragment_path = Path(fragment_raw) if fragment_raw else None
        native_loaded = load_state not in {"", "not-found"}
        installed = definition_present or native_loaded
        if not installed:
            return ExecutorServiceStatus(
                backend="systemd",
                state=ExecutorServiceState.NOT_INSTALLED,
                installed=False,
                running=False,
                service_file=str(service_file),
                log_path=str(self.log_path),
            )

        fragment_mismatch = bool(
            definition_present
            and native_loaded
            and fragment_path is not None
            and fragment_path.is_absolute()
            and fragment_path.absolute() != service_file.absolute()
        )
        detail_parts = [
            part
            for part in (
                f"load={load_state}" if load_state else "",
                f"active={active_state}" if active_state else "",
                f"sub={sub_state}" if sub_state else "",
                f"fragment={fragment_raw}" if fragment_raw else "",
            )
            if part
        ]
        if not definition_present:
            detail_parts.append(
                "service definition missing; reinstall to refresh"
            )
        if fragment_mismatch:
            detail_parts.append(
                "service manager loaded a different definition; reinstall to refresh"
            )
            state = ExecutorServiceState.FAILED
            running = active_state == "active"
        elif definition_present and load_state == "not-found":
            detail_parts.append("service manager has not loaded the definition")
            state = ExecutorServiceState.FAILED
            running = False
        elif active_state == "active":
            state = ExecutorServiceState.RUNNING
            running = True
        elif active_state == "failed" or load_state in {"error", "bad-setting"}:
            state = ExecutorServiceState.FAILED
            running = False
        elif active_state in {"inactive", "deactivating"}:
            state = ExecutorServiceState.STOPPED
            running = False
        else:
            state = ExecutorServiceState.INSTALLED
            running = False
        return ExecutorServiceStatus(
            backend="systemd",
            state=state,
            installed=True,
            running=running,
            detail="; ".join(detail_parts),
            service_file=str(service_file),
            log_path=str(self.log_path),
            runtime_current=(
                self._runtime_current()
                if definition_present and not fragment_mismatch
                else False
            ),
        )

    def _launchd_status(self, service_file: Path) -> ExecutorServiceStatus:
        definition_present = service_file.exists()
        if shutil.which("launchctl") is None:
            if not definition_present:
                return ExecutorServiceStatus(
                    backend="launchd",
                    state=ExecutorServiceState.NOT_INSTALLED,
                    installed=False,
                    running=False,
                    service_file=str(service_file),
                    log_path=str(self.log_path),
                )
            return ExecutorServiceStatus(
                backend="launchd",
                state=ExecutorServiceState.FAILED,
                installed=True,
                running=False,
                detail="launchctl is unavailable",
                service_file=str(service_file),
                log_path=str(self.log_path),
                runtime_current=self._runtime_current(),
            )

        result = self._run(
            ["launchctl", "print", self._launchd_service_target],
            check=False,
        )
        if result.returncode:
            if not definition_present:
                return ExecutorServiceStatus(
                    backend="launchd",
                    state=ExecutorServiceState.NOT_INSTALLED,
                    installed=False,
                    running=False,
                    service_file=str(service_file),
                    log_path=str(self.log_path),
                )
            return ExecutorServiceStatus(
                backend="launchd",
                state=ExecutorServiceState.STOPPED,
                installed=True,
                running=False,
                detail=(result.stderr or result.stdout).strip(),
                service_file=str(service_file),
                log_path=str(self.log_path),
                runtime_current=self._runtime_current(),
            )

        fields: dict[str, str] = {}
        for line in result.stdout.splitlines():
            key, separator, value = line.strip().partition("=")
            if separator:
                fields.setdefault(key.strip().lower(), value.strip())
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
        if not definition_present:
            detail_parts.append(
                "service definition missing; reinstall to refresh"
            )
        return ExecutorServiceStatus(
            backend="launchd",
            state=state,
            installed=True,
            running=running,
            detail="; ".join(detail_parts) or "loaded",
            service_file=str(service_file),
            log_path=str(self.log_path),
            runtime_current=(
                self._runtime_current() if definition_present else False
            ),
        )

    def status(self) -> ExecutorServiceStatus:
        backend = self.backend()
        service_file = self._service_file(backend)
        if backend == "systemd":
            return self._systemd_status(service_file)
        if backend == "launchd":
            return self._launchd_status(service_file)
        return self._windows_status(service_file)

    def logs(self, *, lines: int = _DEFAULT_LOG_LINES) -> str:
        """Return a bounded recent log view from the native backend."""

        if not 1 <= lines <= _MAX_LOG_LINES:
            raise ValueError(f"lines must be between 1 and {_MAX_LOG_LINES}")
        backend = self.backend()
        if backend == "systemd":
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
