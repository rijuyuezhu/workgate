"""Minimal shell dialect differences; process containment is separate (#385)."""

import shlex
import sys
from pathlib import PureWindowsPath

from .host import HostOS, host_os


def default_shell() -> str:
    return "powershell.exe" if host_os() is HostOS.WINDOWS else "/bin/bash"


def default_python() -> str:
    return sys.executable if host_os() is HostOS.WINDOWS else "python3"


def _windows_shell(shell: str) -> str:
    name = PureWindowsPath(shell).name.lower()
    if name in {"powershell", "powershell.exe", "pwsh", "pwsh.exe"}:
        return "powershell"
    if name in {"cmd", "cmd.exe"}:
        return "cmd"
    raise ValueError(f"unsupported Windows shell dialect: {shell}")


def shell_argv(
    shell: str, command: str, *, login: bool = False, system: str | None = None
) -> list[str]:
    """Build argv without invoking another shell to quote a command."""
    if host_os(system) is HostOS.WINDOWS:
        if _windows_shell(shell) == "cmd":
            return [shell, "/D", "/S", "/C", command]
        # PowerShell -Command otherwise folds native child exit codes to 1.
        check_exit = (
            "; if (-not $?) { "
            "if ($null -ne $LASTEXITCODE) { exit $LASTEXITCODE }; exit 1 "
            "}"
        )
        return [
            shell,
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            command + check_exit,
        ]
    return [shell, "-lc" if login else "-c", command]


def shell_join_argv(
    argv: list[str], *, shell: str, system: str | None = None
) -> str:
    """Quote Python script invocation in the configured shell dialect."""
    if host_os(system) is HostOS.WINDOWS:
        if _windows_shell(shell) != "powershell":
            raise ValueError("Python script quoting for cmd.exe is unsupported")
        return "& " + " ".join(
            "'" + arg.replace("'", "''") + "'" for arg in argv
        )
    return shlex.join(argv)


def shell_env_prefix(
    shell: str,
    command: str,
    overrides: dict[str, str],
    *,
    system: str | None = None,
) -> str:
    """Compose already-validated environment assignments for a shell command."""
    if not overrides:
        return command
    if host_os(system) is HostOS.WINDOWS:
        if _windows_shell(shell) != "powershell":
            raise ValueError(
                "environment overrides for cmd.exe are unsupported"
            )
        assignments = []
        for name, value in overrides.items():
            escaped = value.replace("'", "''")
            assignments.append(f"$env:{name} = '{escaped}'")
        return "; ".join([*assignments, command])
    assignments = [
        f"{name}={shlex.quote(value)}" for name, value in overrides.items()
    ]
    return f"{' '.join(assignments)} {command}"
