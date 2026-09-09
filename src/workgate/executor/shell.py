"""Shell command primitives and persistent session operation helpers."""

import asyncio
import contextlib
import contextvars
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from typing import Any

from ..audit import audit
from ..errors import (
    ShellExecutableNotFoundError,
    process_start_not_found_error,
)
from ..schemas.result_models.shell import (
    CommandResult,
    KillPersistentShellOutput,
    ListPersistentShellsOutput,
    ReadPersistentShellOutput,
    ResizePersistentShellOutput,
    RunShellCommandOutput,
    SendPersistentShellInputOutput,
    StartPersistentShellOutput,
)
from ..tool_session.lifecycle import cross_process_lock
from ..tool_session.store import ToolSessionStore
from ..utils.processes import new_process_group_kwargs, user_subprocess_env
from .bounded_runner import bounded_runner_argv
from .config import ExecutorConfig
from .path import (
    relative_display_from_root,
    resolve_path_with_policy,
)
from .terminal import conpty
from .terminal.contracts import (
    PERSISTENT_SHELL_MAX_COLUMNS,
    PERSISTENT_SHELL_MAX_ROWS,
    PERSISTENT_SHELL_MIN_COLUMNS,
    PERSISTENT_SHELL_MIN_ROWS,
)
from .terminal.tmux import require_tmux, resolve_tmux, tmux_env_overrides

GRACEFUL_TERMINATION_TIMEOUT_S = 5
KILL_TERMINATION_TIMEOUT_S = 2
READER_DRAIN_TIMEOUT_S = 2
TOOL_WATCHDOG_SCHEDULING_MARGIN_S = 1
SHELL_TIMEOUT_CLEANUP_GRACE_S = (
    GRACEFUL_TERMINATION_TIMEOUT_S
    + KILL_TERMINATION_TIMEOUT_S
    + READER_DRAIN_TIMEOUT_S
    + TOOL_WATCHDOG_SCHEDULING_MARGIN_S
)
SHELL_TIMEOUT_CLEANUP_TOOL_NAMES = frozenset({"bash", "run_python_code"})
INTERNAL_SHELL_DEFAULT_TIMEOUT_S = 60
INTERNAL_SHELL_MAX_TIMEOUT_S = 3600
_COMMAND_SEMAPHORE: asyncio.Semaphore | None = None
_COMMAND_SEMAPHORE_SIZE: int | None = None
_PERSISTENT_SHELL_CREATION_LOCK: asyncio.Lock | None = None
_PERSISTENT_SHELL_ADMISSION_OWNER = contextvars.ContextVar[object | None](
    "persistent_shell_admission_owner", default=None
)
_PERSISTENT_SHELL_PRESERVE_IDS = contextvars.ContextVar(
    "persistent_shell_preserve_ids", default=frozenset()
)
_TMUX_OWNER_OPTION = "@workgate-session-id"


class PersistentShellCleanupUncertainError(RuntimeError):
    """A failed persistent-shell start may still have a live backend process."""


def _is_frozen_app() -> bool:
    """Return whether this process is running from a frozen app bundle."""
    return bool(getattr(sys, "frozen", False) or getattr(sys, "_MEIPASS", None))


_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass
class TailBuffer:
    """Accumulate bounded process output while tracking how many bytes were dropped from the head."""

    keep_bytes: int
    """Maximum number of output bytes retained in memory."""
    data: bytearray
    """Buffered tail bytes retained from process output."""
    total_bytes: int = 0
    """Total bytes observed before tail truncation."""

    def append(self, chunk: bytes) -> None:
        """Append bytes to the tail buffer and discard the oldest data beyond the configured limit."""
        if not chunk:
            return
        self.total_bytes += len(chunk)
        self.data.extend(chunk)
        overflow = len(self.data) - self.keep_bytes
        if overflow > 0:
            del self.data[:overflow]

    @property
    def truncated(self) -> bool:
        """Report whether any output was dropped while enforcing the buffer limit."""
        return self.total_bytes > len(self.data)


def _shared_tail_bytes(
    stdout: bytes, stderr: bytes, limit: int
) -> tuple[bytes, bytes, bool]:
    """Fit two stream tails into one byte budget without wasting idle-stream capacity."""
    total = len(stdout) + len(stderr)
    if total <= limit:
        return stdout, stderr, False

    stdout_keep = min(len(stdout), limit // 2)
    stderr_keep = min(len(stderr), limit // 2)
    remaining = limit - stdout_keep - stderr_keep
    if remaining > 0:
        stdout_extra = min(remaining, len(stdout) - stdout_keep)
        stdout_keep += stdout_extra
        remaining -= stdout_extra
    if remaining > 0:
        stderr_keep += min(remaining, len(stderr) - stderr_keep)

    return (
        stdout[-stdout_keep:] if stdout_keep else b"",
        stderr[-stderr_keep:] if stderr_keep else b"",
        True,
    )


def check_command_policy(config: ExecutorConfig, command: str) -> None:
    """Reject shell commands matching executor-owned denylist entries."""
    normalized = command.casefold()
    for denied in config.command_denylist:
        if denied and denied.casefold() in normalized:
            raise PermissionError(
                f"Command contains denylisted fragment: {denied!r}"
            )


def _effective_shell_default_timeout_s(config: ExecutorConfig) -> int:
    """Return the effective internal shell default timeout."""
    return max(
        1,
        INTERNAL_SHELL_DEFAULT_TIMEOUT_S,
        config.run_shell_default_timeout_s,
    )


def _effective_shell_max_timeout_s(config: ExecutorConfig) -> int:
    """Return the effective internal shell timeout cap."""
    return max(1, INTERNAL_SHELL_MAX_TIMEOUT_S, config.run_shell_max_timeout_s)


def clamp_timeout(config: ExecutorConfig, timeout_s: int | None) -> int:
    """Clamp requested internal command timeouts to the effective server bounds."""
    timeout = timeout_s or _effective_shell_default_timeout_s(config)
    return max(1, min(timeout, _effective_shell_max_timeout_s(config)))


def run_shell_command_timeout(
    config: ExecutorConfig, timeout_s: int | None
) -> int:
    """Resolve bounded shell command timeout from configured defaults and caps."""
    default = max(1, config.run_shell_default_timeout_s)
    cap = max(1, config.run_shell_max_timeout_s)
    if timeout_s is not None and timeout_s > cap:
        raise ValueError(
            f"timeout_s must be <= {cap} seconds for bounded shell commands; "
            "use bash async or PTY mode for long-running or streaming commands"
        )
    return max(1, min(timeout_s or default, cap))


def _effective_output_limit(
    config: ExecutorConfig, max_output_bytes: int | None = None
) -> int:
    """Resolve the output byte limit requested by a caller against server-wide maximums."""
    configured = max(1, config.max_output_bytes)
    if max_output_bytes is None:
        return configured
    return max(1, min(max_output_bytes, configured))


def _command_semaphore(config: ExecutorConfig) -> asyncio.Semaphore:
    """Return the process-wide semaphore that limits concurrent shell commands."""
    global _COMMAND_SEMAPHORE, _COMMAND_SEMAPHORE_SIZE
    size = max(1, config.max_concurrent_commands)
    if _COMMAND_SEMAPHORE is None or size != _COMMAND_SEMAPHORE_SIZE:
        _COMMAND_SEMAPHORE = asyncio.Semaphore(size)
        _COMMAND_SEMAPHORE_SIZE = size
    return _COMMAND_SEMAPHORE


def _persistent_shell_creation_lock() -> asyncio.Lock:
    """Serialize persistent-shell capacity checks and creation."""
    global _PERSISTENT_SHELL_CREATION_LOCK
    if _PERSISTENT_SHELL_CREATION_LOCK is None:
        _PERSISTENT_SHELL_CREATION_LOCK = asyncio.Lock()
    return _PERSISTENT_SHELL_CREATION_LOCK


@contextlib.asynccontextmanager
async def _persistent_shell_admission_lock():
    """Hold the backend admission lock once for the current task."""
    task = asyncio.current_task()
    if task is None:
        raise RuntimeError(
            "persistent shell admission requires an asyncio task"
        )
    if _PERSISTENT_SHELL_ADMISSION_OWNER.get() is task:
        yield
        return
    backend = "conpty" if _use_conpty_persistent_shell_backend() else "tmux"
    async with cross_process_lock("persistent-shell-admission", backend):
        token = _PERSISTENT_SHELL_ADMISSION_OWNER.set(task)
        try:
            yield
        finally:
            _PERSISTENT_SHELL_ADMISSION_OWNER.reset(token)


def _subprocess_env() -> dict[str, str]:
    """Return the environment exposed to user shell commands."""
    return user_subprocess_env(frozen=_is_frozen_app())


def _effective_shell_executable(config: ExecutorConfig) -> str:
    """Return a usable configured shell, adapting the POSIX default on Windows."""
    configured = str(config.shell_executable).strip()
    if os.name == "nt" and configured in {"", "/bin/bash"}:
        return os.environ.get("COMSPEC") or "cmd.exe"
    return configured or os.environ.get("SHELL") or "/bin/sh"


def _shell_command_args(shell: str, command: str) -> list[str]:
    """Build native argv for POSIX shells, PowerShell, or cmd.exe."""
    name = os.path.basename(shell).lower()
    if name in {"powershell", "powershell.exe", "pwsh", "pwsh.exe"}:
        return [shell, "-NoProfile", "-NonInteractive", "-Command", command]
    if name in {"cmd", "cmd.exe"}:
        return [shell, "/D", "/S", "/C", command]
    return [shell, "-lc", command]


def _shell_join_argv(argv: list[str]) -> str:
    """Render argv for the native shell without losing Windows quoting."""
    return (
        subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)
    )


def _effective_python_executable(config: ExecutorConfig) -> str:
    """Return a usable Python executable, adapting the POSIX default on Windows."""
    configured = str(config.python_bin).strip()
    if os.name == "nt" and configured in {"", "python3"}:
        return sys.executable or "python"
    return configured or sys.executable


def _validated_env_overrides(env: dict[str, str] | None) -> dict[str, str]:
    """Validate and normalize caller-provided subprocess environment overrides."""
    if not env:
        return {}
    normalized: dict[str, str] = {}
    for name, value in env.items():
        if not _ENV_NAME_RE.match(name):
            raise ValueError(f"Invalid environment variable name: {name!r}")
        normalized[name] = str(value)
    return normalized


def _command_with_env(
    config: ExecutorConfig, command: str, env: dict[str, str] | None
) -> str:
    """Prefix PTY/job commands with shell-native environment assignments."""
    overrides = _validated_env_overrides(env)
    if not overrides:
        return command
    shell_name = os.path.basename(_effective_shell_executable(config)).lower()
    if shell_name in {"powershell", "powershell.exe", "pwsh", "pwsh.exe"}:
        assignments = [
            f"$env:{name}='{value.replace(chr(39), chr(39) * 2)}'"
            for name, value in overrides.items()
        ]
        return f"{'; '.join(assignments)}; {command}"
    if shell_name in {"cmd", "cmd.exe"}:
        assignments = [
            f'set "{name}={value}"' for name, value in overrides.items()
        ]
        return f"{' && '.join(assignments)} && {command}"
    assignments = [
        f"{name}={shlex.quote(value)}" for name, value in overrides.items()
    ]
    return f"{' '.join(assignments)} {command}"


def _bounded_runner_argv(shell: str, command: str) -> list[str]:
    """Build the internal descendant-reaping runner invocation."""
    return bounded_runner_argv(
        shell,
        command,
        frozen=_is_frozen_app(),
    )


async def _spawn_process(
    config: ExecutorConfig,
    command: str,
    cwd: str,
    env: dict[str, str] | None = None,
) -> asyncio.subprocess.Process:
    """Start a bounded shell command in a descendant-reaping process group."""
    shell = _effective_shell_executable(config)
    child_env = _subprocess_env()
    child_env.update(_validated_env_overrides(env))
    process_group = new_process_group_kwargs()
    common: dict[str, Any] = {
        "cwd": cwd,
        "env": child_env,
        "stdin": asyncio.subprocess.DEVNULL,
        "stdout": asyncio.subprocess.PIPE,
        "stderr": asyncio.subprocess.PIPE,
        **process_group,
    }
    shell_candidate = shell
    if not os.path.isabs(shell) and os.path.dirname(shell):
        shell_candidate = os.path.abspath(os.path.join(cwd, shell))
    resolved_shell = shutil.which(shell_candidate, path=child_env.get("PATH"))
    if resolved_shell is None:
        raise ShellExecutableNotFoundError(
            shell,
            command,
            cwd,
            "configured shell executable was not found or is not executable",
        )
    shell_name = os.path.basename(resolved_shell).lower()
    try:
        if os.name == "nt" and shell_name in {"cmd", "cmd.exe"}:
            return await asyncio.create_subprocess_shell(
                command,
                executable=resolved_shell,
                **common,
            )
        if os.name == "nt":
            return await asyncio.create_subprocess_exec(
                *_shell_command_args(resolved_shell, command),
                **common,
            )
        return await asyncio.create_subprocess_exec(
            *_bounded_runner_argv(resolved_shell, command),
            **common,
        )
    except FileNotFoundError as exc:
        raise process_start_not_found_error(
            exc,
            executable=shell,
            command=command,
            cwd=cwd,
        ) from exc


async def _read_stream_tail(
    stream: asyncio.StreamReader | None, tail: TailBuffer
) -> None:
    """Continuously read a process stream into a bounded tail buffer."""
    if stream is None:
        return
    while True:
        chunk = await stream.read(65536)
        if not chunk:
            return
        tail.append(chunk)


async def _wait_for_process_exit(
    proc: asyncio.subprocess.Process, timeout_s: int
) -> bool:
    """Wait for process completion while converting timeout into structured result state."""
    try:
        await asyncio.wait_for(proc.wait(), timeout=timeout_s)
        return True
    except TimeoutError:
        return False


async def _terminate_process_group(proc: asyncio.subprocess.Process) -> str:
    """Terminate an entire shell process group, escalating to kill when graceful shutdown times out."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except Exception:
        proc.terminate()

    output = await _wait_for_process_exit(proc, GRACEFUL_TERMINATION_TIMEOUT_S)
    if output:
        return ""

    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:
        proc.kill()

    output = await _wait_for_process_exit(proc, KILL_TERMINATION_TIMEOUT_S)
    if output:
        return ""
    return "Process did not exit after SIGKILL"


async def _finish_reader_tasks(
    tasks: list[asyncio.Task[None]], timeout_s: float = READER_DRAIN_TIMEOUT_S
) -> None:
    """Let stream-reader tasks drain briefly before cancelling unfinished readers."""
    try:
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=timeout_s)
    except TimeoutError:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def run_shell(
    config: ExecutorConfig,
    command: str,
    cwd: str = ".",
    timeout_s: int | None = None,
    max_output_bytes: int | None = None,
    env: dict[str, str] | None = None,
) -> CommandResult:
    """Execute a shell command under explicit executor-owned policy."""
    check_command_policy(config, command)
    resolved_cwd = resolve_path_with_policy(
        cwd,
        workspace_root=config.workspace_root,
        allow_full_control=config.allow_full_control,
        path_denylist=config.path_denylist,
        must_exist=True,
    )
    start = time.time()
    audit("run_shell_command_start", command=command, cwd=str(resolved_cwd))
    timeout = clamp_timeout(config, timeout_s)

    proc: asyncio.subprocess.Process | None = None
    timed_out = False
    termination_error = ""
    output_limit = _effective_output_limit(config, max_output_bytes)
    stdout_tail = TailBuffer(output_limit, bytearray())
    stderr_tail = TailBuffer(output_limit, bytearray())
    reader_tasks: list[asyncio.Task[None]] = []

    async def spawn_and_wait() -> None:
        nonlocal proc
        proc = await _spawn_process(config, command, str(resolved_cwd), env)
        reader_tasks.extend(
            [
                asyncio.create_task(
                    _read_stream_tail(proc.stdout, stdout_tail)
                ),
                asyncio.create_task(
                    _read_stream_tail(proc.stderr, stderr_tail)
                ),
            ]
        )
        await proc.wait()

    semaphore = _command_semaphore(config)
    acquired = False
    try:
        try:
            await asyncio.wait_for(semaphore.acquire(), timeout=timeout)
            acquired = True
            elapsed = max(0.0, time.time() - start)
            remaining_timeout = max(0.001, timeout - elapsed)
            await asyncio.wait_for(spawn_and_wait(), timeout=remaining_timeout)
        except TimeoutError:
            timed_out = True
            if proc is None:
                reader_tasks = []
                termination_error = "Timed out while starting subprocess"
            else:
                termination_error = await _terminate_process_group(proc)
        except asyncio.CancelledError:
            if proc is not None:
                await asyncio.shield(_terminate_process_group(proc))
            raise
    finally:
        if acquired:
            semaphore.release()

    if reader_tasks:
        await _finish_reader_tasks(reader_tasks)
    if termination_error:
        stderr_tail.append(termination_error.encode())

    stdout_b, stderr_b, total_truncated = _shared_tail_bytes(
        bytes(stdout_tail.data), bytes(stderr_tail.data), output_limit
    )
    stdout = stdout_b.decode(errors="replace")
    stderr = stderr_b.decode(errors="replace")
    truncated = (
        stdout_tail.truncated or stderr_tail.truncated or total_truncated
    )
    duration_ms = int((time.time() - start) * 1000)
    result = CommandResult(
        ok=(proc is not None and proc.returncode == 0 and not timed_out),
        exit_code=proc.returncode if proc is not None else None,
        timed_out=timed_out,
        duration_ms=duration_ms,
        cwd=relative_display_from_root(resolved_cwd, config.workspace_root),
        command=command,
        stdout=stdout,
        stderr=stderr,
        truncated=truncated,
    )
    audit(
        "run_shell_command_end",
        command=command,
        cwd=str(resolved_cwd),
        exit_code=proc.returncode if proc is not None else None,
        timed_out=timed_out,
        duration_ms=duration_ms,
        truncated=truncated,
    )
    return result


async def run_shell_command_execute(
    config: ExecutorConfig,
    command: str,
    cwd: str = ".",
    timeout_s: int | None = None,
    max_output_bytes: int | None = None,
    env: dict[str, str] | None = None,
) -> RunShellCommandOutput:
    """Execute a bounded shell command under explicit executor-owned policy."""
    result = await run_shell(
        config,
        command,
        cwd,
        run_shell_command_timeout(config, timeout_s),
        max_output_bytes,
        env,
    )
    return RunShellCommandOutput(**result.model_dump())


async def _spawn_exec_process(
    argv: list[str],
    cwd: str,
    env: dict[str, str] | None = None,
) -> asyncio.subprocess.Process:
    """Start one direct executable without routing through the configured shell."""
    child_env = _subprocess_env()
    child_env.update(_validated_env_overrides(env))
    process_group = new_process_group_kwargs()
    command = _shell_join_argv(argv)
    try:
        return await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            env=child_env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **process_group,
        )
    except FileNotFoundError as exc:
        raise process_start_not_found_error(
            exc,
            executable=str(argv[0]),
            command=command,
            cwd=cwd,
        ) from exc


async def _run_exec(
    config: ExecutorConfig,
    argv: list[str],
    *,
    cwd: str = ".",
    timeout_s: int | None = None,
    env: dict[str, str] | None = None,
) -> CommandResult:
    """Run one trusted argv under explicit executor-owned policy."""
    if not argv:
        raise ValueError("argv must not be empty")
    command = _shell_join_argv(argv)
    check_command_policy(config, command)
    resolved_cwd = resolve_path_with_policy(
        cwd,
        workspace_root=config.workspace_root,
        allow_full_control=config.allow_full_control,
        path_denylist=config.path_denylist,
        must_exist=True,
    )
    start = time.time()
    audit("run_shell_command_start", command=command, cwd=str(resolved_cwd))
    timeout = clamp_timeout(config, timeout_s)
    proc: asyncio.subprocess.Process | None = None
    timed_out = False
    termination_error = ""
    output_limit = _effective_output_limit(config)
    stdout_tail = TailBuffer(output_limit, bytearray())
    stderr_tail = TailBuffer(output_limit, bytearray())
    reader_tasks: list[asyncio.Task[None]] = []

    async def spawn_and_wait() -> None:
        nonlocal proc
        proc = await _spawn_exec_process(argv, str(resolved_cwd), env)
        reader_tasks.extend(
            [
                asyncio.create_task(
                    _read_stream_tail(proc.stdout, stdout_tail)
                ),
                asyncio.create_task(
                    _read_stream_tail(proc.stderr, stderr_tail)
                ),
            ]
        )
        await proc.wait()

    semaphore = _command_semaphore(config)
    acquired = False
    try:
        try:
            await asyncio.wait_for(semaphore.acquire(), timeout=timeout)
            acquired = True
            elapsed = max(0.0, time.time() - start)
            await asyncio.wait_for(
                spawn_and_wait(), timeout=max(0.001, timeout - elapsed)
            )
        except TimeoutError:
            timed_out = True
            if proc is None:
                reader_tasks = []
                termination_error = "Timed out while starting subprocess"
            else:
                termination_error = await _terminate_process_group(proc)
        except asyncio.CancelledError:
            if proc is not None:
                await asyncio.shield(_terminate_process_group(proc))
            raise
    finally:
        if acquired:
            semaphore.release()

    if reader_tasks:
        await _finish_reader_tasks(reader_tasks)
    if termination_error:
        stderr_tail.append(termination_error.encode())
    stdout_b, stderr_b, total_truncated = _shared_tail_bytes(
        bytes(stdout_tail.data), bytes(stderr_tail.data), output_limit
    )
    duration_ms = int((time.time() - start) * 1000)
    result = CommandResult(
        ok=(proc is not None and proc.returncode == 0 and not timed_out),
        exit_code=proc.returncode if proc is not None else None,
        timed_out=timed_out,
        duration_ms=duration_ms,
        cwd=relative_display_from_root(resolved_cwd, config.workspace_root),
        command=command,
        stdout=stdout_b.decode(errors="replace"),
        stderr=stderr_b.decode(errors="replace"),
        truncated=(
            stdout_tail.truncated or stderr_tail.truncated or total_truncated
        ),
    )
    audit(
        "run_shell_command_end",
        command=command,
        cwd=str(resolved_cwd),
        exit_code=proc.returncode if proc is not None else None,
        timed_out=timed_out,
        duration_ms=duration_ms,
        truncated=result.truncated,
    )
    return result


def _tmux_session_cwd(args: list[str]) -> str:
    """Return the new-session cwd used to resolve a relative configured shell."""
    if args and args[0] == "new-session":
        try:
            return args[args.index("-c") + 1]
        except ValueError, IndexError:
            pass
    return "."


def _resolved_tmux_shell(config: ExecutorConfig, session_cwd: str = ".") -> str:
    """Resolve the configured persistent shell without trusting account $SHELL."""
    configured = os.path.expanduser(_effective_shell_executable(config))
    candidate = shutil.which(configured, path=_subprocess_env().get("PATH"))
    if candidate:
        return candidate
    if os.path.isabs(configured):
        return configured
    return os.path.abspath(os.path.join(session_cwd, configured))


def _tmux_session_name(name: str | None = None) -> str:
    """Normalize user-facing shell names into the tmux naming scheme used by the server."""
    base = name or f"mcp-{uuid.uuid4().hex[:8]}"
    cleaned = re.sub(r"[^A-Za-z0-9_.-]", "-", base.strip())[:64].strip(".-")
    return cleaned or f"mcp-{uuid.uuid4().hex[:8]}"


async def tmux(
    config: ExecutorConfig, args: list[str], timeout_s: int = 10
) -> CommandResult:
    """Run tmux directly with a resolved configured shell in its environment."""
    selection = require_tmux(config.tmux_bin)
    if selection.path is None:  # pragma: no cover - require_tmux enforces this.
        raise RuntimeError("tmux executable resolution returned no path")
    return await _run_exec(
        config,
        [selection.path, *args],
        cwd=".",
        timeout_s=timeout_s,
        env={
            **tmux_env_overrides(),
            "SHELL": _resolved_tmux_shell(config, _tmux_session_cwd(args)),
        },
    )


def _tmux_server_absent(result: CommandResult) -> bool:
    """Return whether tmux conclusively reported that no server is running."""
    detail = f"{result.stderr}\n{result.stdout}".lower()
    return (
        "no server running" in detail
        or "failed to connect to server" in detail
        or (
            "error connecting to " in detail
            and "no such file or directory" in detail
        )
    )


def _persistent_shell_ids(output: Any) -> set[str]:
    """Extract shell ids from native models and compatibility-shim dictionaries."""
    data = output.model_dump() if hasattr(output, "model_dump") else output
    if isinstance(data, dict):
        items = data.get("shells", data.get("sessions", []))
    else:
        items = getattr(output, "shells", getattr(output, "sessions", []))
    shell_ids: set[str] = set()
    for item in items:
        value = (
            item.get("shell_id") or item.get("session_id")
            if isinstance(item, dict)
            else getattr(item, "shell_id", None)
            or getattr(item, "session_id", None)
        )
        if value:
            shell_ids.add(str(value))
    return shell_ids


def _use_conpty_persistent_shell_backend() -> bool:
    """Return whether persistent shells should use the Windows ConPTY backend."""
    return os.name == "nt"


async def _cleanup_failed_tmux_start(
    config: ExecutorConfig, shell_id: str
) -> None:
    """Kill a newly created tmux session despite creator cancellation."""
    cleanup = asyncio.create_task(
        tmux(config, ["kill-session", "-t", f"={shell_id}"], timeout_s=5)
    )
    while not cleanup.done():
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            continue
    result = cleanup.result()
    if not result.ok and not _tmux_server_absent(result):
        raise RuntimeError(
            result.stderr
            or result.stdout
            or f"failed to clean up persistent shell {shell_id}"
        )


async def _start_persistent_shell_locked(
    config: ExecutorConfig,
    store: ToolSessionStore,
    cwd: str = ".",
    name: str | None = None,
    command: str | None = None,
    *,
    owner_session_id: str | None = None,
    shell_id: str | None = None,
) -> StartPersistentShellOutput:
    """Start a persistent shell while the creation lock is held."""
    resolved_cwd = resolve_path_with_policy(
        cwd,
        workspace_root=config.workspace_root,
        allow_full_control=config.allow_full_control,
        path_denylist=config.path_denylist,
        must_exist=True,
    )
    shell_id = shell_id or _tmux_session_name(name)
    active_shell_ids = await authoritative_persistent_shell_ids_execute(
        config, store
    )
    if active_shell_ids is None:
        raise RuntimeError(
            "Persistent shell inventory is unavailable; refusing to start a new "
            "session until the backend can report active shells authoritatively."
        )
    if shell_id in active_shell_ids:
        raise RuntimeError(
            f"Persistent shell id already exists: {shell_id}. Choose a different "
            "name or stop the existing shell first."
        )
    max_sessions = max(1, config.max_tmux_sessions)
    if len(active_shell_ids) >= max_sessions:
        active = ", ".join(sorted(active_shell_ids))
        raise RuntimeError(
            f"Refusing to start more than {max_sessions} persistent shell "
            f"sessions; active shell ids: {active or '<unknown>'}. Use "
            "list_persistent_shells and kill_persistent_shell to release capacity."
        )
    if _use_conpty_persistent_shell_backend():
        if not conpty.is_available():
            raise RuntimeError(
                "pywinpty is required for persistent shells on Windows"
            )
        initial = conpty.initial_command(command, config.shell_executable)
        check_command_policy(config, initial)
        try:
            return await conpty.start_shell(
                shell_id=shell_id,
                cwd=resolved_cwd,
                command=command,
                owner_session_id=owner_session_id,
                shell_executable=config.shell_executable,
            )
        except conpty.ConPtyCleanupUncertainError as exc:
            raise PersistentShellCleanupUncertainError(str(exc)) from exc

    configured_shell = _resolved_tmux_shell(config, str(resolved_cwd))
    if (
        shutil.which(configured_shell, path=_subprocess_env().get("PATH"))
        is None
    ):
        raise ShellExecutableNotFoundError(
            configured_shell,
            command or configured_shell,
            resolved_cwd,
            "configured shell executable was not found or is not executable",
        )
    initial = command or configured_shell
    check_command_policy(config, initial)
    cmd = [
        "new-session",
        "-d",
        "-s",
        shell_id,
        "-c",
        str(resolved_cwd),
    ]
    if command is not None:
        cmd.append(command)
    if owner_session_id is not None:
        cmd.extend(
            [
                ";",
                "set-option",
                "-t",
                shell_id,
                _TMUX_OWNER_OPTION,
                owner_session_id,
            ]
        )
    try:
        result = await tmux(config, cmd)
        if not result.ok:
            raise RuntimeError(result.stderr or result.stdout)
        if command is None:
            alive = await tmux(
                config, ["has-session", "-t", f"={shell_id}"], timeout_s=5
            )
            if not alive.ok:
                detail = (alive.stderr or alive.stdout).strip()
                message = f"Persistent shell session exited during startup: {shell_id}"
                if detail:
                    message += f" ({detail})"
                raise RuntimeError(message)
        audit(
            "start_persistent_shell",
            shell_id=shell_id,
            cwd=str(resolved_cwd),
            command=initial,
            backend="tmux",
        )
        return StartPersistentShellOutput(
            shell_id=shell_id,
            cwd=relative_display_from_root(resolved_cwd, config.workspace_root),
            command=initial,
            backend="tmux",
        )
    except BaseException:
        try:
            await _cleanup_failed_tmux_start(config, shell_id)
        except Exception as cleanup_error:
            raise PersistentShellCleanupUncertainError(
                f"persistent shell startup failed and cleanup was not confirmed: {shell_id}"
            ) from cleanup_error
        raise


async def start_persistent_shell_execute(
    config: ExecutorConfig,
    store: ToolSessionStore,
    cwd: str = ".",
    name: str | None = None,
    command: str | None = None,
    *,
    owner_session_id: str | None = None,
) -> StartPersistentShellOutput:
    """Start one persistent shell without racing the configured capacity."""
    async with (
        _persistent_shell_creation_lock(),
        _persistent_shell_admission_lock(),
    ):
        reserved_shell_id = (
            _tmux_session_name(name) if owner_session_id is not None else None
        )
        session_store = None
        reservation_added = False
        if reserved_shell_id is not None:
            assert owner_session_id is not None
            session_store = store
            reservation_added = session_store.reserve_persistent_shell(
                owner_session_id,
                reserved_shell_id,
                exclusive=True,
            )
        try:
            token = _PERSISTENT_SHELL_PRESERVE_IDS.set(
                frozenset(
                    {reserved_shell_id} if reserved_shell_id is not None else ()
                )
            )
            try:
                return await _start_persistent_shell_locked(
                    config,
                    store,
                    cwd,
                    name,
                    command,
                    owner_session_id=owner_session_id,
                    shell_id=reserved_shell_id,
                )
            finally:
                _PERSISTENT_SHELL_PRESERVE_IDS.reset(token)
        except PersistentShellCleanupUncertainError:
            # Keep the reservation so pruning and teardown remain fail closed
            # until authoritative backend reconciliation is possible.
            raise
        except BaseException:
            if reserved_shell_id is not None and reservation_added:
                assert owner_session_id is not None
                assert session_store is not None
                try:
                    session_store.release_session_persistent_shell(
                        owner_session_id, reserved_shell_id
                    )
                except Exception as rollback_error:
                    raise RuntimeError(
                        "persistent shell startup failed and durable ownership "
                        "rollback was not confirmed: "
                        f"{reserved_shell_id}"
                    ) from rollback_error
            raise


async def send_persistent_shell_input_execute(
    config: ExecutorConfig,
    shell_id: str,
    input_text: str,
    enter: bool = True,
) -> SendPersistentShellInputOutput:
    """Send input to a persistent shell, optionally appending Enter."""
    if _use_conpty_persistent_shell_backend():
        return await conpty.send_shell(shell_id, input_text, enter)
    if input_text:
        result = await tmux(
            config, ["send-keys", "-l", "-t", shell_id, input_text]
        )
        if not result.ok:
            raise RuntimeError(result.stderr or result.stdout)
    if enter:
        result = await tmux(config, ["send-keys", "-t", shell_id, "Enter"])
        if not result.ok:
            raise RuntimeError(result.stderr or result.stdout)
    audit(
        "send_persistent_shell_input",
        shell_id=shell_id,
        bytes=len(input_text.encode()),
        enter=enter,
        backend="tmux",
    )
    return SendPersistentShellInputOutput(
        shell_id=shell_id,
        sent_bytes=len(input_text.encode()),
        enter=enter,
    )


def _validate_persistent_shell_size(cols: int, rows: int) -> tuple[int, int]:
    columns = int(cols)
    lines = int(rows)
    if (
        not PERSISTENT_SHELL_MIN_COLUMNS
        <= columns
        <= PERSISTENT_SHELL_MAX_COLUMNS
    ):
        raise ValueError(
            f"cols must be between {PERSISTENT_SHELL_MIN_COLUMNS} and "
            f"{PERSISTENT_SHELL_MAX_COLUMNS}"
        )
    if not PERSISTENT_SHELL_MIN_ROWS <= lines <= PERSISTENT_SHELL_MAX_ROWS:
        raise ValueError(
            f"rows must be between {PERSISTENT_SHELL_MIN_ROWS} and "
            f"{PERSISTENT_SHELL_MAX_ROWS}"
        )
    return columns, lines


async def resize_persistent_shell_execute(
    config: ExecutorConfig, shell_id: str, cols: int, rows: int
) -> ResizePersistentShellOutput:
    """Resize one persistent terminal when supported by its backend."""
    columns, lines = _validate_persistent_shell_size(cols, rows)
    if _use_conpty_persistent_shell_backend():
        return await conpty.resize_shell(shell_id, columns, lines)
    result = await tmux(
        config,
        [
            "resize-window",
            "-t",
            shell_id,
            "-x",
            str(columns),
            "-y",
            str(lines),
        ],
    )
    if not result.ok:
        raise RuntimeError(result.stderr or result.stdout)
    audit(
        "resize_persistent_shell",
        shell_id=shell_id,
        cols=columns,
        rows=lines,
        backend="tmux",
    )
    return ResizePersistentShellOutput(
        shell_id=shell_id,
        cols=columns,
        rows=lines,
        resized=True,
        backend="tmux",
    )


async def read_persistent_shell_output_execute(
    config: ExecutorConfig,
    shell_id: str,
    lines: int = 200,
    *,
    preserve_ansi: bool = False,
) -> ReadPersistentShellOutput:
    """Read recent output from a persistent shell."""
    if _use_conpty_persistent_shell_backend():
        return await conpty.read_shell(
            shell_id,
            lines,
            preserve_ansi=preserve_ansi,
        )
    capture_args = ["capture-pane", "-p"]
    if preserve_ansi:
        capture_args.append("-e")
    capture_args.extend(["-t", shell_id, "-S", f"-{max(1, lines)}"])
    result = await tmux(config, capture_args)
    if not result.ok:
        raise RuntimeError(result.stderr or result.stdout)
    audit(
        "read_persistent_shell_output",
        shell_id=shell_id,
        lines=lines,
        preserve_ansi=preserve_ansi,
        backend="tmux",
    )
    return ReadPersistentShellOutput(
        shell_id=shell_id,
        output=result.stdout,
        lines=lines,
        backend="tmux",
    )


async def kill_persistent_shell_execute(
    config: ExecutorConfig,
    store: ToolSessionStore,
    shell_id: str,
) -> KillPersistentShellOutput:
    """Terminate a persistent shell by its normalized shell id."""
    if _use_conpty_persistent_shell_backend():
        result = await conpty.kill_shell(shell_id)
        if result.killed:
            store.release_persistent_shell(shell_id)
        return result
    result = await tmux(config, ["kill-session", "-t", shell_id])
    audit(
        "kill_persistent_shell",
        shell_id=shell_id,
        ok=result.ok,
        backend="tmux",
    )
    output = KillPersistentShellOutput(
        shell_id=shell_id,
        killed=result.ok,
        stderr=result.stderr,
        backend="tmux",
    )
    if output.killed:
        store.release_persistent_shell(shell_id)
    return output


async def list_owned_persistent_shell_ids_execute(
    config: ExecutorConfig,
    store: ToolSessionStore,
    owner_session_id: str,
) -> list[str] | None:
    """Return owned shell ids, or None when discovery is non-authoritative."""
    if _use_conpty_persistent_shell_backend():
        try:
            session = store.require_session(owner_session_id)
            durable_ids = set(session.persistent_shell_ids)
            if not durable_ids:
                return []
            local_ids = set(await conpty.list_owned_shell_ids(owner_session_id))
            authoritative_ids = conpty.authoritative_shell_ids()
        except Exception:
            return None
        if authoritative_ids is None:
            return None
        dead_durable_ids = durable_ids - authoritative_ids
        for shell_id in sorted(dead_durable_ids):
            try:
                store.release_session_persistent_shell(
                    owner_session_id, shell_id
                )
            except Exception:
                return None
        durable_ids -= dead_durable_ids
        if not durable_ids.issubset(local_ids):
            return None
        return sorted(local_ids)
    selection = resolve_tmux(config.tmux_bin)
    if selection.path is None and selection.source == "unavailable":
        # A session that has never durably reserved a shell has nothing to
        # clean up even when the tmux backend itself is unavailable. Keep
        # fail-closed behavior for any session with durable shell ownership.
        try:
            session = store.require_session(owner_session_id)
        except Exception:
            return None
        return [] if not session.persistent_shell_ids else None
    result = await tmux(
        config,
        [
            "list-sessions",
            "-F",
            f"#{{session_name}}\t#{{{_TMUX_OWNER_OPTION}}}",
        ],
        timeout_s=5,
    )
    if not result.ok:
        return [] if _tmux_server_absent(result) else None
    owned: list[str] = []
    for line in result.stdout.splitlines():
        shell_id, _, owner = line.partition("\t")
        if shell_id and owner == owner_session_id:
            owned.append(shell_id)
    return owned


async def _persistent_shell_inventory_execute(
    config: ExecutorConfig,
    store: ToolSessionStore,
    *,
    preserve_shell_ids: set[str] | None = None,
) -> tuple[ListPersistentShellsOutput, bool]:
    """Return the shell inventory and whether an empty result is authoritative."""
    if _use_conpty_persistent_shell_backend():
        output = await conpty.list_shells()
        return output, False
    selection = resolve_tmux(config.tmux_bin)
    if selection.path is None and selection.source == "unavailable":
        return ListPersistentShellsOutput(shells=[]), False
    result = await tmux(
        config,
        [
            "list-sessions",
            "-F",
            "#{session_name}\t#{session_created}\t#{session_attached}",
        ],
        timeout_s=5,
    )
    if not result.ok:
        if _tmux_server_absent(result):
            store.reconcile_persistent_shells(set(preserve_shell_ids or ()))
            return ListPersistentShellsOutput(shells=[]), True
        return ListPersistentShellsOutput(shells=[]), False
    shells = []
    for line in result.stdout.splitlines():
        parts = line.split("\t")
        if parts:
            shells.append(
                {
                    "shell_id": parts[0],
                    "created": parts[1] if len(parts) > 1 else None,
                    "attached": parts[2] if len(parts) > 2 else None,
                    "backend": "tmux",
                }
            )
    output = ListPersistentShellsOutput(shells=shells)
    store.reconcile_persistent_shells(
        _persistent_shell_ids(output) | set(preserve_shell_ids or ())
    )
    return output, True


async def _authoritative_persistent_shell_ids_locked(
    config: ExecutorConfig,
    store: ToolSessionStore,
    *,
    preserve_shell_ids: set[str] | None = None,
) -> set[str] | None:
    """Return live shell ids while the backend admission lock is held."""
    if _use_conpty_persistent_shell_backend():
        try:
            active = conpty.authoritative_shell_ids()
            if active is None:
                return None
            store.reconcile_persistent_shells(
                active | set(preserve_shell_ids or ())
            )
            return active
        except Exception:
            return None
    try:
        output, authoritative = await _persistent_shell_inventory_execute(
            config,
            store,
            preserve_shell_ids=preserve_shell_ids,
        )
    except Exception:
        return None
    if not authoritative:
        return None
    return _persistent_shell_ids(output)


async def authoritative_persistent_shell_ids_execute(
    config: ExecutorConfig, store: ToolSessionStore
) -> set[str] | None:
    """Return live shell ids, or None when the backend inventory is uncertain."""
    async with _persistent_shell_admission_lock():
        preserve_shell_ids = (
            set(_PERSISTENT_SHELL_PRESERVE_IDS.get())
            if _PERSISTENT_SHELL_ADMISSION_OWNER.get() is asyncio.current_task()
            else set()
        )
        return await _authoritative_persistent_shell_ids_locked(
            config,
            store,
            preserve_shell_ids=preserve_shell_ids,
        )


async def list_persistent_shells_execute(
    config: ExecutorConfig, store: ToolSessionStore
) -> ListPersistentShellsOutput:
    """List active persistent shells managed by this executor."""
    async with _persistent_shell_admission_lock():
        output, _authoritative = await _persistent_shell_inventory_execute(
            config, store
        )
        return output
