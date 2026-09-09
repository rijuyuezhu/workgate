"""Executor-owned runtime and capability orientation for shared sessions."""

import os
import platform
import re
import shlex
import shutil
import struct
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

from .. import __version__
from ..schemas.result_models.session import (
    SessionCapabilitiesEnvironment,
    SessionEnvironment,
    SessionPolicyEnvironment,
    SessionRuntimeEnvironment,
    SessionToolProbe,
    SessionToolsEnvironment,
    SessionWorkspaceEnvironment,
)
from ..version import package_version
from .config import ExecutorConfig
from .terminal.conpty import is_available as conpty_available
from .terminal.tmux import resolve_tmux

_PROBE_TIMEOUT_S = 1.5
_CACHE_TTL_S = 5.0
_VERSION_RE = re.compile(
    r"(?<![0-9])([0-9]+(?:\.[0-9]+){1,3}(?:[-+._]?[A-Za-z0-9]+)*)"
)
_SAFE_TOKEN_RE = re.compile(r"[^A-Za-z0-9._+-]+")
_CACHE_LOCK = threading.Lock()
_TOOL_CACHE: dict[
    tuple[object, ...], tuple[float, SessionToolsEnvironment]
] = {}


@dataclass(frozen=True)
class _CommandProbe:
    name: str
    command: tuple[str, ...] | None
    source: str | None


def _safe_token(value: str, fallback: str, maximum: int = 80) -> str:
    normalized = _SAFE_TOKEN_RE.sub("_", value.strip())[:maximum].strip("_")
    return normalized or fallback


def _normalized_os(system: str) -> str:
    lowered = system.strip().lower()
    if lowered == "darwin":
        return "macos"
    if lowered.startswith("win"):
        return "windows"
    return _safe_token(lowered, "unknown")


def _resolve_command(value: str) -> tuple[str, ...] | None:
    try:
        parts = shlex.split(value, posix=os.name != "nt")
    except ValueError:
        return None
    if not parts:
        return None
    executable = parts[0]
    path = Path(executable).expanduser()
    resolved = str(path) if path.is_file() else shutil.which(executable)
    if not resolved:
        return None
    return (resolved, *parts[1:])


def _version_command(command: tuple[str, ...], kind: str) -> tuple[str, ...]:
    executable_name = Path(command[0]).name.lower()
    if kind == "shell" and executable_name in {"cmd", "cmd.exe"}:
        return (*command, "/d", "/c", "ver")
    if kind == "tmux":
        return (*command, "-V")
    if kind == "shell" and executable_name in {
        "powershell",
        "powershell.exe",
        "pwsh",
        "pwsh.exe",
    }:
        return (
            *command,
            "-NoLogo",
            "-NoProfile",
            "-Command",
            "$PSVersionTable.PSVersion.ToString()",
        )
    return (*command, "--version")


def _extract_version(output: str) -> str | None:
    match = _VERSION_RE.search(output[:4096])
    return match.group(1)[:80] if match else None


def _probe_command(spec: _CommandProbe) -> SessionToolProbe:
    if spec.command is None:
        return SessionToolProbe(
            available=False,
            status="missing",
            source=spec.source,
        )
    try:
        result = subprocess.run(  # noqa: S603
            list(_version_command(spec.command, spec.name)),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=_PROBE_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return SessionToolProbe(
            available=False,
            status="timeout",
            source=spec.source,
        )
    except OSError:
        return SessionToolProbe(
            available=False,
            status="error",
            source=spec.source,
        )
    output = f"{result.stdout[:2048]}\n{result.stderr[:2048]}"
    return SessionToolProbe(
        available=result.returncode == 0,
        status="available" if result.returncode == 0 else "error",
        version=_extract_version(output),
        source=spec.source,
    )


def _tool_probe_context(
    config: ExecutorConfig,
) -> tuple[tuple[object, ...], tuple[_CommandProbe, ...]]:
    tmux = resolve_tmux(config.tmux_bin)
    probes = (
        _CommandProbe(
            "shell", _resolve_command(config.shell_executable), "configured"
        ),
        _CommandProbe("git", _resolve_command(config.git_bin), "configured"),
        _CommandProbe("ripgrep", _resolve_command(config.rg_bin), "configured"),
        _CommandProbe(
            "tmux",
            (tmux.path,) if tmux.path else None,
            tmux.source,
        ),
    )
    key: tuple[object, ...] = (
        os.name,
        *(probe.command for probe in probes),
        *(probe.source for probe in probes),
    )
    return key, probes


def _collect_tools(config: ExecutorConfig) -> SessionToolsEnvironment:
    key, probes = _tool_probe_context(config)
    now = time.monotonic()
    with _CACHE_LOCK:
        cached = _TOOL_CACHE.get(key)
        if cached is not None and now - cached[0] <= _CACHE_TTL_S:
            return cached[1].model_copy(deep=True)

    results: dict[str, SessionToolProbe] = {}
    with ThreadPoolExecutor(max_workers=len(probes)) as executor:
        futures = {
            executor.submit(_probe_command, probe): probe.name
            for probe in probes
        }
        for future in as_completed(futures):
            name = futures[future]
            try:
                results[name] = future.result()
            except Exception:
                results[name] = SessionToolProbe(
                    available=False,
                    status="error",
                )

    tools = SessionToolsEnvironment(
        shell=results["shell"],
        git=results["git"],
        ripgrep=results["ripgrep"],
        tmux=results["tmux"],
    )
    with _CACHE_LOCK:
        _TOOL_CACHE.clear()
        _TOOL_CACHE[key] = (now, tools.model_copy(deep=True))
    return tools


def _runtime_environment() -> SessionRuntimeEnvironment:
    return SessionRuntimeEnvironment(
        workgate_version=__version__,
        package_version=package_version(),
        python_implementation=_safe_token(
            platform.python_implementation(), "unknown"
        ),
        python_version=platform.python_version()[:80],
        runtime_kind="frozen" if getattr(sys, "frozen", False) else "source",
        os=_normalized_os(platform.system()),
        release=_safe_token(platform.release(), "unknown"),
        architecture=_safe_token(platform.machine(), "unknown"),
        process_bits=64 if struct.calcsize("P") * 8 >= 64 else 32,
    )


def _policy_environment(config: ExecutorConfig) -> SessionPolicyEnvironment:
    return SessionPolicyEnvironment(
        full_control=config.allow_full_control,
        shell_default_timeout_s=config.run_shell_default_timeout_s,
        shell_max_timeout_s=config.run_shell_max_timeout_s,
        max_output_bytes=config.max_output_bytes,
        max_jobs=config.max_jobs,
        max_job_log_bytes=config.max_job_log_bytes,
        max_session_snapshots=config.max_session_snapshots,
        max_session_snapshot_bytes=config.max_session_snapshot_bytes,
        max_file_read_bytes=config.max_file_read_bytes,
        max_file_write_bytes=config.max_file_write_bytes,
        max_view_image_bytes=config.max_view_image_bytes,
        max_search_results=config.max_grep_results,
        max_glob_results=config.max_glob_results,
        max_tree_entries=config.max_tree_entries,
        max_directory_entries=config.max_directory_entries,
        max_concurrent_commands=config.max_concurrent_commands,
        max_persistent_shells=config.max_tmux_sessions,
        max_transfer_archive_entries=config.max_transfer_archive_entries,
        max_transfer_unpacked_bytes=config.max_transfer_unpacked_bytes,
    )


def collect_executor_session_environment(
    config: ExecutorConfig,
    *,
    workdir: str,
) -> SessionEnvironment:
    """Collect bounded orientation from executor-owned config and machine state."""
    try:
        tools = _collect_tools(config)
    except Exception:
        unavailable = SessionToolProbe(available=False, status="error")
        tools = SessionToolsEnvironment(
            shell=unavailable,
            git=unavailable,
            ripgrep=unavailable,
            tmux=unavailable,
        )
    conpty = conpty_available()
    return SessionEnvironment(
        runtime=_runtime_environment(),
        workspace=SessionWorkspaceEnvironment(
            workspace_root=str(config.workspace_root),
            workdir=workdir,
        ),
        tools=tools,
        capabilities=SessionCapabilitiesEnvironment(
            raw_pty=conpty or tools.tmux.available,
            conpty=conpty,
        ),
        policy=_policy_environment(config),
    )
