"""Local executor service-manager integration.

This module is deliberately host-local.  It manages only the operating-system
launcher for the existing ``workgate executor run`` runtime and never owns
pairing or executor credentials.
"""

import hashlib
import json
import os
import platform
import plistlib
import posixpath
import shutil
import subprocess
import sys
from contextlib import suppress
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import Any

from ..app_paths import ensure_private_directory
from ..config.executor import EXECUTOR_SETTING_NAMES, ExecutorConfig
from ..persistence import FileStateStore
from ..platform.host import HostOS, host_os
from ..platform.windows_task import WindowsUserTask
from ..protocol.executor import ExecutorRuntimeOwnership
from ..utils.private_files import (
    atomic_write_private_bytes,
    atomic_write_private_text,
)
from ..utils.systemd import systemd_quote, systemd_user_unit_path
from ..version import __version__
from .profile import ExecutorProfileStore
from .runtime_update import (
    ExecutorRuntimeStateStore,
    ExecutorRuntimeUpdateStatus,
)

_SERVICE_NAME = "workgate-executor"
_LAUNCHD_LABEL = "com.workgate.executor"
_WINDOWS_TASK_NAME = "Workgate Executor"
_WINDOWS_TASK_DESCRIPTION = "Managed by Workgate: executor"
_WINDOWS_SUCCESS_RESULTS = {0, 0x41300, 0x41301, 0x41303, 0x41306}
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
    runtime_current: bool = False
    runtime_ownership: ExecutorRuntimeOwnership = (
        ExecutorRuntimeOwnership.UNKNOWN
    )
    runtime_update_status: ExecutorRuntimeUpdateStatus = (
        ExecutorRuntimeUpdateStatus.IDLE
    )
    runtime_update_target_version: str | None = None
    runtime_update_detail: str | None = None


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


def _executor_service_settings(config: ExecutorConfig) -> dict[str, Any]:
    """Snapshot only user-configurable executor settings.

    Control-plane auth settings and executor bearer credentials are never
    copied into the managed-service configuration.
    """

    return {
        name: _json_value(getattr(config, name))
        for name in sorted(EXECUTOR_SETTING_NAMES)
    }


def _settings_payload_digest(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _executor_service_settings_digest(config: ExecutorConfig) -> str:
    return _settings_payload_digest(_executor_service_settings(config))


class ExecutorServiceManager:
    """Manage one paired executor through the host's per-user service manager."""

    def __init__(
        self,
        config: ExecutorConfig,
        *,
        home: Path | None = None,
        environ: dict[str, str] | None = None,
        system: str | None = None,
        executable: Path | None = None,
    ) -> None:
        self.config = config
        self.home = (Path.home() if home is None else Path(home)).expanduser()
        self.environ = dict(os.environ if environ is None else environ)
        self.system = platform.system() if system is None else system
        self.executable = Path(
            sys.executable if executable is None else executable
        )
        self.state_store = FileStateStore(lambda: self.config.state_dir)

    @property
    def service_dir(self) -> Path:
        return self.config.state_dir / "executor" / "service"

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

    def _windows_task(self) -> WindowsUserTask:
        return WindowsUserTask(
            _WINDOWS_TASK_NAME,
            description=_WINDOWS_TASK_DESCRIPTION,
            run=self._run,
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

    def backend(self, *, require_available: bool = False) -> str:
        if host_os(self.system) is HostOS.LINUX:
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
        if host_os(self.system) is HostOS.MACOS:
            if require_available and shutil.which("launchctl") is None:
                raise UnsupportedExecutorServiceError(
                    "launchctl is required on macOS"
                )
            return "launchd"
        if host_os(self.system) is HostOS.WINDOWS:
            if require_available and not WindowsUserTask.available():
                raise UnsupportedExecutorServiceError(
                    "Windows Task Scheduler requires PowerShell"
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
            _executor_service_settings(self.config),
            indent=2,
            sort_keys=True,
        )
        atomic_write_private_text(self.config_path, payload + "\n")

    def _launcher_source(
        self, runtime_ownership: ExecutorRuntimeOwnership
    ) -> str:
        config_path = str(self.config_path.resolve())
        log_path = str(self.log_path.resolve())
        return (
            '"""Private Workgate managed-executor launcher. Generated; do not edit."""\n'
            "import contextlib\n"
            "import os\n"
            "import traceback\n\n"
            f"CONFIG_PATH = {config_path!r}\n\n"
            "for _name in list(os.environ):\n"
            "    if _name.startswith('WORKGATE_'):\n"
            "        os.environ.pop(_name, None)\n"
            "os.environ['PYTHONUNBUFFERED'] = '1'\n"
            "def _run():\n"
            "    from workgate.main import main\n"
            "    main(['executor', 'run', '--managed-service', "
            f"'--runtime-ownership', {runtime_ownership.value!r}, "
            "'--config', CONFIG_PATH])\n"
            "if os.name == 'nt':\n"
            f"    with open({log_path!r}, 'a', encoding='utf-8', buffering=1) as _log:\n"
            "        with contextlib.redirect_stdout(_log), contextlib.redirect_stderr(_log):\n"
            "            try:\n"
            "                _run()\n"
            "            except BaseException:\n"
            "                traceback.print_exc()\n"
            "                raise\n"
            "else:\n"
            "    _run()\n"
        )

    def _write_launcher(
        self,
        command: list[str] | None = None,
        *,
        runtime_ownership: ExecutorRuntimeOwnership | None = None,
    ) -> None:
        frozen = bool(getattr(sys, "frozen", False))
        ownership = runtime_ownership or self._runtime_ownership()
        if command is None:
            command = self._runtime_command(ownership)
        if frozen:
            self.launcher_path.unlink(missing_ok=True)
            return
        atomic_write_private_text(
            self.launcher_path, self._launcher_source(ownership)
        )

    def _runtime_ownership(self) -> ExecutorRuntimeOwnership:
        store = ExecutorRuntimeStateStore(self.state_store)
        state = store.load()
        return (
            state.ownership
            if state is not None
            else store.install_state().ownership
        )

    def _runtime_command(
        self,
        runtime_ownership: ExecutorRuntimeOwnership | None = None,
    ) -> list[str]:
        # Preserve a virtualenv/pipx interpreter symlink. Resolving it can escape
        # the environment that actually contains the Workgate installation.
        executable = self.executable.absolute()
        ownership = runtime_ownership or self._runtime_ownership()
        if getattr(sys, "frozen", False):
            return [
                str(executable),
                "executor",
                "run",
                "--managed-service",
                "--runtime-ownership",
                ownership.value,
                "--config",
                str(self.config_path.resolve()),
            ]
        if host_os(self.system) is HostOS.WINDOWS:
            pythonw = executable.with_name("pythonw.exe")
            if not pythonw.is_file():
                raise UnsupportedExecutorServiceError(
                    "Windows source service requires pythonw.exe to avoid a console"
                )
            executable = pythonw
        return [str(executable), str(self.launcher_path.resolve())]

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
            "version": 3,
            "backend": backend,
            "workgate_version": __version__,
            "settings_sha256": _executor_service_settings_digest(self.config),
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

    def _installed_settings_digest(self) -> str | None:
        try:
            payload = json.loads(self.config_path.read_text(encoding="utf-8"))
        except OSError, ValueError:
            return None
        if not isinstance(payload, dict):
            return None
        return _settings_payload_digest(payload)

    def _runtime_launcher_present(self) -> bool:
        if not getattr(sys, "frozen", False):
            return self.launcher_path.is_file()
        return True

    def _runtime_current(self) -> bool:
        payload = self._read_metadata()
        expected_settings = _executor_service_settings_digest(self.config)
        runtime = ExecutorRuntimeStateStore(self.state_store).load()
        version_current = (
            runtime is not None
            and runtime.ownership is ExecutorRuntimeOwnership.CONTROL_MANAGED
        ) or (
            payload is not None
            and payload.get("workgate_version") == __version__
        )
        if (
            payload is None
            or payload.get("version") != 3
            or payload.get("backend") != self.backend()
            or not version_current
            or payload.get("settings_sha256") != expected_settings
            or self._installed_settings_digest() != expected_settings
            or not self._runtime_launcher_present()
        ):
            return False
        command = payload.get("command")
        if not isinstance(command, list) or not all(
            isinstance(item, str) for item in command
        ):
            return False
        try:
            return command == self._runtime_command()
        except UnsupportedExecutorServiceError:
            return False

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

    def _systemd_environment_lines(self) -> list[str]:
        values = [
            "PYTHONUNBUFFERED=1",
            f"PATH={self._managed_posix_path(_POSIX_DEFAULT_PATH_DIRS)}",
        ]
        return [
            f"Environment={systemd_quote(value, escape_dollar=False)}"
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
            return systemd_user_unit_path(
                result.stdout, f"{_SERVICE_NAME}.service"
            )
        except ValueError as exc:
            raise RuntimeError(str(exc)) from exc

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
                + " ".join(systemd_quote(item) for item in command),
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

    def install(
        self,
        *,
        runtime_ownership: ExecutorRuntimeOwnership | None = None,
    ) -> ExecutorServiceStatus:
        """Install or refresh the native per-user service and start it."""

        self._ensure_paired()
        if host_os(self.system) is HostOS.WINDOWS and getattr(
            sys, "frozen", False
        ):
            raise UnsupportedExecutorServiceError(
                "Windows frozen service requires a windowless executable; "
                "this frozen build is not yet service-capable"
            )
        runtime_store = ExecutorRuntimeStateStore(self.state_store)
        runtime_state = runtime_store.install_state(runtime_ownership)
        backend = self.backend(require_available=True)
        self._write_service_config()
        command = self._runtime_command(runtime_state.ownership)
        self._write_launcher(
            command,
            runtime_ownership=runtime_state.ownership,
        )

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

        elif backend == "scheduled-task":
            task = self._windows_task()
            task.install(command, cwd=str(self.service_dir.resolve()))
            self._write_metadata(backend, command)
            task.start()

        runtime_store.service_runtime_state(runtime_state.ownership)
        return self.status()

    def activate_windows_runtime(self, old: Path, new: Path) -> None:
        """Switch the existing owned task to a verified next executable."""
        if host_os(self.system) is not HostOS.WINDOWS:
            raise UnsupportedExecutorServiceError(
                "native Windows task required"
            )
        metadata = self._read_metadata()
        command = None if metadata is None else metadata.get("command")
        if (
            metadata is None
            or metadata.get("backend") != "scheduled-task"
            or not isinstance(command, list)
            or not command
            or not all(isinstance(arg, str) for arg in command)
            or Path(command[0]) != old
        ):
            raise RuntimeError(
                "managed Windows executable does not match installed service"
            )
        task = self._windows_task()
        task.switch_runtime(
            expected=str(old), replacement=str(new), restart=True
        )
        try:
            self._write_metadata("scheduled-task", [str(new), *command[1:]])
        except Exception:
            try:
                task.switch_runtime(
                    expected=str(new), replacement=str(old), restart=False
                )
            except Exception as exc:
                raise RuntimeError(
                    "Windows task metadata update failed and task rollback failed"
                ) from exc
            raise
        # Released old versions can be pruned on a later successful update.
        # Never touch the current image or the verified new target.
        for candidate in new.parent.glob(".workgate.runtime.*.exe"):
            if candidate in {old, new}:
                continue
            with suppress(OSError):  # Still mapped by a Windows process.
                candidate.unlink()

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
        elif backend == "scheduled-task":
            self._windows_task().uninstall()
        for path in (
            self.config_path,
            self.launcher_path,
            self.metadata_path,
            self.state_store.layout.executor_runtime_path,
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
        elif backend == "scheduled-task":
            self._windows_task().start()
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
        elif backend == "scheduled-task":
            self._windows_task().stop()
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
        elif backend == "scheduled-task":
            task = self._windows_task()
            task.stop()
            task.start()
        return self.status()

    def _require_installed(self) -> None:
        status = self.status()
        if not status.installed:
            raise RuntimeError(
                "executor service is not installed; run "
                "`workgate executor install-service` first"
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

    def _windows_status(self) -> ExecutorServiceStatus:
        task = self._windows_task()
        metadata = self.metadata_path.is_file()
        try:
            native = task.status()
        except RuntimeError as exc:
            return ExecutorServiceStatus(
                backend="scheduled-task",
                state=ExecutorServiceState.FAILED,
                installed=metadata,
                running=False,
                detail=str(exc),
                log_path=str(self.log_path),
            )
        if native is None:
            return ExecutorServiceStatus(
                backend="scheduled-task",
                state=(
                    ExecutorServiceState.FAILED
                    if metadata
                    else ExecutorServiceState.NOT_INSTALLED
                ),
                installed=False,
                running=False,
                detail=(
                    "scheduled task missing; reinstall to refresh"
                    if metadata
                    else ""
                ),
                log_path=str(self.log_path),
            )
        name = str(native["state"]).lower()
        code = native.get("last_result")
        if name == "running":
            state = ExecutorServiceState.RUNNING
        elif name == "disabled" or (
            isinstance(code, int) and code not in _WINDOWS_SUCCESS_RESULTS
        ):
            state = ExecutorServiceState.FAILED
        elif name == "queued":
            state = ExecutorServiceState.INSTALLED
        else:
            state = ExecutorServiceState.STOPPED
        return ExecutorServiceStatus(
            backend="scheduled-task",
            state=state,
            installed=True,
            running=state is ExecutorServiceState.RUNNING,
            detail=f"task={name}; last result={code}"
            + ("; service metadata missing" if not metadata else ""),
            log_path=str(self.log_path),
            runtime_current=(self._runtime_current() if metadata else False),
        )

    def status(self) -> ExecutorServiceStatus:
        backend = self.backend()
        if backend == "systemd":
            status = self._systemd_status(self.systemd_unit_path)
        elif backend == "launchd":
            status = self._launchd_status(self.launchd_plist_path)
        else:
            status = self._windows_status()
        runtime = ExecutorRuntimeStateStore(self.state_store).load()
        if runtime is None:
            return status
        return replace(
            status,
            runtime_ownership=runtime.ownership,
            runtime_update_status=runtime.update_status,
            runtime_update_target_version=runtime.update_target_version,
            runtime_update_detail=runtime.update_detail,
        )

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
    "ExecutorServiceManager",
    "ExecutorServiceState",
    "ExecutorServiceStatus",
    "UnsupportedExecutorServiceError",
]
