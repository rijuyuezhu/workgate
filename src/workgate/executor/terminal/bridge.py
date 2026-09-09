"""Connection-scoped raw PTY bridges for persistent shells.

The public persistent-shell tools intentionally remain snapshot based. This
module is an internal Human UI transport. POSIX bridges start a short-lived
``tmux attach-session`` client; Windows bridges subscribe to the existing
ConPTY session's shared reader. Closing either bridge removes only the raw
client and preserves the underlying persistent shell.
"""

import asyncio
import base64
import contextlib
import errno
import os
import re
import secrets
import select
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from ...protocol.terminal import (
    TERMINAL_BRIDGE_BACKEND,
    TERMINAL_BRIDGE_MAX_CHUNK_BYTES,
    TerminalBridgeBusyError,
    TerminalBridgeError,
    TerminalBridgeNotFoundError,
    TerminalBridgeUnsupportedError,
)
from . import conpty
from .contracts import (
    PERSISTENT_SHELL_MAX_COLUMNS,
    PERSISTENT_SHELL_MAX_ROWS,
    PERSISTENT_SHELL_MIN_COLUMNS,
    PERSISTENT_SHELL_MIN_ROWS,
)
from .tmux import detached_tmux_env, require_tmux

TERMINAL_BRIDGE_MAX_WAIT_MS = 1_000
_TERMINAL_BRIDGE_HARD_IDLE_TIMEOUT_S = 300
_BRIDGE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{20,128}$")
_SHELL_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


def _bounded_int(
    value: Any,
    *,
    default: int,
    minimum: int,
    maximum: int,
    label: str,
) -> int:
    if value is None or value == "":
        normalized = default
    else:
        try:
            normalized = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{label} must be an integer") from exc
    if not minimum <= normalized <= maximum:
        raise ValueError(f"{label} must be between {minimum} and {maximum}")
    return normalized


def _shell_id(value: Any) -> str:
    normalized = str(value or "")
    if not _SHELL_ID_PATTERN.fullmatch(normalized):
        raise ValueError(
            "shell_id must be 1-64 characters using letters, digits, '.', '_' or '-'"
        )
    return normalized


def _bridge_id(value: Any) -> str:
    normalized = str(value or "")
    if not _BRIDGE_ID_PATTERN.fullmatch(normalized):
        raise TerminalBridgeNotFoundError("Terminal bridge is unavailable")
    return normalized


def _terminal_size(cols: Any, rows: Any) -> tuple[int, int]:
    columns = _bounded_int(
        cols,
        default=120,
        minimum=PERSISTENT_SHELL_MIN_COLUMNS,
        maximum=PERSISTENT_SHELL_MAX_COLUMNS,
        label="cols",
    )
    lines = _bounded_int(
        rows,
        default=36,
        minimum=PERSISTENT_SHELL_MIN_ROWS,
        maximum=PERSISTENT_SHELL_MAX_ROWS,
        label="rows",
    )
    return columns, lines


def _decode_chunk(value: Any) -> bytes:
    encoded = str(value or "")
    if len(encoded) > ((TERMINAL_BRIDGE_MAX_CHUNK_BYTES + 2) // 3) * 4 + 4:
        raise ValueError("Terminal bridge input is too large")
    try:
        data = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError("Terminal bridge input must be valid base64") from exc
    if len(data) > TERMINAL_BRIDGE_MAX_CHUNK_BYTES:
        raise ValueError("Terminal bridge input is too large")
    return data


def _use_conpty_bridge() -> bool:
    return os.name == "nt"


class _TerminalProcess(Protocol):
    backend: str
    shell_id: str

    def read_wait(self, max_bytes: int, wait_ms: int) -> tuple[bytes, bool]: ...

    def write_all(self, data: bytes) -> None: ...

    def resize(self, cols: int, rows: int) -> bool: ...

    def close_sync(self) -> None: ...


class _UnixTmuxAttach:
    """One POSIX PTY hosting a tmux attach client."""

    backend = TERMINAL_BRIDGE_BACKEND

    def __init__(self, shell_id: str, cols: int, rows: int) -> None:
        if os.name == "nt":  # pragma: no cover - exercised by platform guards.
            raise TerminalBridgeUnsupportedError(
                "Raw tmux terminal bridges require POSIX PTY support"
            )

        import fcntl
        import pty
        import struct
        import termios

        self._fcntl = fcntl
        self._struct = struct
        self._termios = termios
        self._close_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._closed = False
        self.shell_id = shell_id
        self.master_fd = -1

        tmux_bin = require_tmux().path
        assert tmux_bin is not None
        tmux_env = detached_tmux_env()
        check = subprocess.run(
            [tmux_bin, "has-session", "-t", f"={shell_id}"],
            capture_output=True,
            check=False,
            env=tmux_env,
            timeout=10,
        )
        if check.returncode != 0:
            detail = (
                (check.stderr or check.stdout)
                .decode("utf-8", errors="replace")
                .strip()
            )
            raise TerminalBridgeNotFoundError(
                detail or f"Persistent shell not found: {shell_id}"
            )

        master_fd, slave_fd = pty.openpty()
        self.master_fd = master_fd
        try:
            self.resize(cols, rows)
            env = tmux_env
            env["TERM"] = "xterm-256color"
            env["COLORTERM"] = "truecolor"
            self.process = subprocess.Popen(
                [tmux_bin, "attach-session", "-t", f"={shell_id}"],
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                env=env,
                close_fds=True,
                start_new_session=True,
                bufsize=0,
            )
        except Exception:
            with contextlib.suppress(OSError):
                os.close(master_fd)
            raise
        finally:
            with contextlib.suppress(OSError):
                os.close(slave_fd)
        os.set_blocking(master_fd, False)

    def resize(self, cols: int, rows: int) -> bool:
        if self._closed or self.master_fd < 0:
            raise TerminalBridgeNotFoundError("Terminal bridge is unavailable")
        winsize = self._struct.pack("HHHH", rows, cols, 0, 0)
        self._fcntl.ioctl(
            self.master_fd,
            self._termios.TIOCSWINSZ,
            winsize,
        )
        process = getattr(self, "process", None)
        if process is not None and process.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGWINCH)
        return True

    def read_wait(self, max_bytes: int, wait_ms: int) -> tuple[bytes, bool]:
        if self._closed or self.master_fd < 0:
            return b"", True
        timeout = max(0.0, wait_ms / 1000.0)
        readable, _, _ = select.select([self.master_fd], [], [], timeout)
        if not readable:
            return b"", self.process.poll() is not None
        try:
            data = os.read(self.master_fd, max_bytes)
        except BlockingIOError:
            return b"", self.process.poll() is not None
        except OSError as exc:
            if exc.errno in {errno.EIO, errno.EBADF}:
                return b"", True
            raise
        return data, not data

    def _write_all(self, data: bytes) -> None:
        if (
            self._closed
            or self.master_fd < 0
            or self.process.poll() is not None
        ):
            raise TerminalBridgeNotFoundError("Terminal bridge is unavailable")
        remaining = memoryview(data)
        with self._write_lock:
            while remaining:
                try:
                    written = os.write(self.master_fd, remaining)
                except BlockingIOError:
                    select.select([], [self.master_fd], [], 0.1)
                    continue
                except OSError as exc:
                    if exc.errno in {errno.EIO, errno.EBADF}:
                        raise TerminalBridgeNotFoundError(
                            "Terminal bridge is unavailable"
                        ) from exc
                    raise
                if written <= 0:
                    raise OSError("PTY write made no progress")
                remaining = remaining[written:]

    def write_all(self, data: bytes) -> None:
        if data:
            self._write_all(data)

    def close_sync(self) -> None:
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
            master_fd = self.master_fd
            self.master_fd = -1
            process = getattr(self, "process", None)

        if master_fd >= 0:
            with contextlib.suppress(OSError):
                os.close(master_fd)
        if process is None or process.poll() is not None:
            return
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=2)


@dataclass
class _Bridge:
    bridge_id: str
    shell_id: str
    process: _TerminalProcess
    cols: int
    rows: int
    idle_timeout_s: float
    last_activity: float = field(default_factory=time.monotonic)
    timer: threading.Timer | None = None


class TerminalBridgeRegistry:
    """Own raw-terminal bridge live state for one process runtime."""

    def __init__(
        self,
        *,
        idle_timeout_s: int = _TERMINAL_BRIDGE_HARD_IDLE_TIMEOUT_S,
        max_connections: int = 8,
    ) -> None:
        configured_idle = int(idle_timeout_s)
        self.idle_timeout_s = float(
            _TERMINAL_BRIDGE_HARD_IDLE_TIMEOUT_S
            if configured_idle <= 0
            else max(
                60,
                min(configured_idle, _TERMINAL_BRIDGE_HARD_IDLE_TIMEOUT_S),
            )
        )
        self.max_connections = max(1, min(int(max_connections), 128))
        self.bridges: dict[str, _Bridge] = {}
        self.shell_bridges: dict[str, str] = {}
        self.pending_shells: set[str] = set()
        self.lock = threading.Lock()
        self._operations: dict[asyncio.Task[Any], int] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._closed = False

    async def start(self) -> None:
        """Bind async operation tracking to the owning event loop."""
        if self._closed:
            raise RuntimeError("terminal bridge registry is closed")
        loop = asyncio.get_running_loop()
        if self._loop is not None and self._loop is not loop:
            raise RuntimeError(
                "terminal bridge registry cannot span event loops"
            )
        self._loop = loop

    def stop_admission(self) -> None:
        """Reject new bridge operations while shutdown drains existing work."""
        self._closed = True

    def require_open(self) -> None:
        """Reject work after runtime shutdown starts."""
        if self._closed:
            raise TerminalBridgeError("terminal bridge registry is closed")

    def begin_operation(self) -> asyncio.Task[Any] | None:
        """Track one in-flight async bridge operation on the owning loop."""
        self.require_open()
        loop = asyncio.get_running_loop()
        if self._loop is None:
            self._loop = loop
        elif self._loop is not loop:
            raise RuntimeError(
                "terminal bridge registry cannot span event loops"
            )
        task = asyncio.current_task()
        if task is not None:
            self._operations[task] = self._operations.get(task, 0) + 1
        return task

    def end_operation(self, task: asyncio.Task[Any] | None) -> None:
        """Release one nested operation reference for the current task."""
        if task is None:
            return
        count = self._operations.get(task, 0)
        if count <= 1:
            self._operations.pop(task, None)
        else:
            self._operations[task] = count - 1

    async def aclose(self) -> None:
        """Cancel in-flight operations, then close every registered bridge."""
        loop = asyncio.get_running_loop()
        if self._loop is not None and self._loop is not loop:
            raise RuntimeError(
                "terminal bridge registry must close on its owning event loop"
            )
        self.stop_admission()

        current = asyncio.current_task()
        operations = tuple(
            task
            for task in self._operations
            if task is not current and not task.done()
        )
        for task in operations:
            task.cancel()
        if operations:
            await asyncio.gather(*operations, return_exceptions=True)

        with self.lock:
            bridges = list(self.bridges.values())
            self.bridges.clear()
            self.shell_bridges.clear()
            self.pending_shells.clear()
            for bridge in bridges:
                if bridge.timer is not None:
                    bridge.timer.cancel()
                    bridge.timer = None
        self._operations.clear()

        if bridges:
            results = await asyncio.gather(
                *(
                    asyncio.to_thread(bridge.process.close_sync)
                    for bridge in bridges
                ),
                return_exceptions=True,
            )
            errors = [
                result
                for result in results
                if isinstance(result, BaseException)
            ]
            if errors:
                raise RuntimeError(
                    f"failed to close {len(errors)} terminal bridge(s)"
                ) from errors[0]


_TERMINAL_BRIDGE_REGISTRY: TerminalBridgeRegistry | None = None


def configure_terminal_bridge_registry(
    registry: TerminalBridgeRegistry | None,
) -> TerminalBridgeRegistry | None:
    """Install a non-owning compatibility binding and return the previous one."""
    global _TERMINAL_BRIDGE_REGISTRY
    previous = _TERMINAL_BRIDGE_REGISTRY
    _TERMINAL_BRIDGE_REGISTRY = registry
    return previous


def _bridge_registry() -> TerminalBridgeRegistry:
    registry = _TERMINAL_BRIDGE_REGISTRY
    if registry is None:
        raise RuntimeError(
            "terminal bridge registry is not configured; start TerminalRuntime"
        )
    return registry


def _schedule_expiry_locked(
    registry: TerminalBridgeRegistry,
    bridge: _Bridge,
    delay: float | None = None,
) -> None:
    timer = threading.Timer(
        max(1.0, delay if delay is not None else bridge.idle_timeout_s),
        _expire_bridge,
        args=(registry, bridge.bridge_id),
    )
    timer.daemon = True
    bridge.timer = timer
    timer.start()


def _expire_bridge(registry: TerminalBridgeRegistry, bridge_id: str) -> None:
    process: _TerminalProcess | None = None
    with registry.lock:
        bridge = registry.bridges.get(bridge_id)
        if bridge is None:
            return
        remaining = bridge.idle_timeout_s - (
            time.monotonic() - bridge.last_activity
        )
        if remaining > 0:
            _schedule_expiry_locked(registry, bridge, remaining)
            return
        registry.bridges.pop(bridge_id, None)
        if registry.shell_bridges.get(bridge.shell_id) == bridge_id:
            registry.shell_bridges.pop(bridge.shell_id, None)
        process = bridge.process
        bridge.timer = None
    if process is not None:
        process.close_sync()


def _lookup_bridge(
    registry: TerminalBridgeRegistry,
    bridge_id: Any,
    *,
    touch: bool = True,
) -> _Bridge:
    normalized = _bridge_id(bridge_id)
    with registry.lock:
        bridge = registry.bridges.get(normalized)
        if bridge is None:
            raise TerminalBridgeNotFoundError("Terminal bridge is unavailable")
        if touch:
            bridge.last_activity = time.monotonic()
        return bridge


def _detach_bridge(
    registry: TerminalBridgeRegistry, bridge_id: Any
) -> _Bridge | None:
    normalized = _bridge_id(bridge_id)
    with registry.lock:
        bridge = registry.bridges.pop(normalized, None)
        if bridge is None:
            return None
        if registry.shell_bridges.get(bridge.shell_id) == normalized:
            registry.shell_bridges.pop(bridge.shell_id, None)
        if bridge.timer is not None:
            bridge.timer.cancel()
            bridge.timer = None
        return bridge


async def open_terminal_bridge_execute(
    shell_id: str,
    cols: int = 120,
    rows: int = 36,
) -> dict[str, Any]:
    """Open one exclusive raw PTY attachment to a persistent shell."""
    registry = _bridge_registry()
    operation = registry.begin_operation()
    try:
        normalized_shell = _shell_id(shell_id)
        columns, lines = _terminal_size(cols, rows)

        maximum = registry.max_connections
        with registry.lock:
            if (
                normalized_shell in registry.shell_bridges
                or normalized_shell in registry.pending_shells
            ):
                raise TerminalBridgeBusyError(
                    f"Persistent shell {normalized_shell} already has a raw terminal client"
                )
            if len(registry.bridges) + len(registry.pending_shells) >= maximum:
                raise TerminalBridgeBusyError(
                    "Too many active raw terminal bridges"
                )
            registry.pending_shells.add(normalized_shell)

        bridge_id = secrets.token_urlsafe(32)
        process: _TerminalProcess | None = None
        try:
            if _use_conpty_bridge():
                if not conpty.is_available():
                    raise TerminalBridgeUnsupportedError(
                        "Raw terminal bridges on Windows require pywinpty"
                    )

                async def create_process() -> _TerminalProcess:
                    try:
                        return await asyncio.to_thread(
                            conpty.open_raw_attachment,
                            normalized_shell,
                            bridge_id,
                            columns,
                            lines,
                        )
                    except RuntimeError as exc:
                        raise TerminalBridgeNotFoundError(str(exc)) from exc

            else:

                async def create_process() -> _TerminalProcess:
                    return await asyncio.to_thread(
                        _UnixTmuxAttach,
                        normalized_shell,
                        columns,
                        lines,
                    )

            creation_task = asyncio.create_task(create_process())
            try:
                process = await asyncio.shield(creation_task)
            except asyncio.CancelledError:
                while not creation_task.done():
                    try:
                        await asyncio.shield(creation_task)
                    except asyncio.CancelledError:
                        continue
                if not creation_task.cancelled():
                    with contextlib.suppress(BaseException):
                        process = creation_task.result()
                raise

            assert process is not None
            bridge = _Bridge(
                bridge_id=bridge_id,
                shell_id=normalized_shell,
                process=process,
                cols=columns,
                rows=lines,
                idle_timeout_s=registry.idle_timeout_s,
            )
            with registry.lock:
                registry.pending_shells.discard(normalized_shell)
                registry.require_open()
                if normalized_shell in registry.shell_bridges:
                    raise TerminalBridgeBusyError(
                        f"Persistent shell {normalized_shell} already has a raw terminal client"
                    )
                registry.bridges[bridge_id] = bridge
                registry.shell_bridges[normalized_shell] = bridge_id
                _schedule_expiry_locked(registry, bridge)
            process = None
            return {
                "bridge_id": bridge_id,
                "shell_id": normalized_shell,
                "cols": columns,
                "rows": lines,
                "backend": bridge.process.backend,
            }
        finally:
            with registry.lock:
                registry.pending_shells.discard(normalized_shell)
            if process is not None:
                await asyncio.to_thread(process.close_sync)
    finally:
        registry.end_operation(operation)


async def read_terminal_bridge_execute(
    bridge_id: str,
    max_bytes: int = TERMINAL_BRIDGE_MAX_CHUNK_BYTES,
    wait_ms: int = 250,
) -> dict[str, Any]:
    """Read one bounded raw byte chunk from a terminal bridge."""
    registry = _bridge_registry()
    operation = registry.begin_operation()
    try:
        limit = _bounded_int(
            max_bytes,
            default=TERMINAL_BRIDGE_MAX_CHUNK_BYTES,
            minimum=1,
            maximum=TERMINAL_BRIDGE_MAX_CHUNK_BYTES,
            label="max_bytes",
        )
        wait = _bounded_int(
            wait_ms,
            default=250,
            minimum=0,
            maximum=TERMINAL_BRIDGE_MAX_WAIT_MS,
            label="wait_ms",
        )
        bridge = _lookup_bridge(registry, bridge_id)
        data, eof = await asyncio.to_thread(
            bridge.process.read_wait, limit, wait
        )
        if len(data) > limit:
            raise TerminalBridgeError(
                "Terminal bridge returned an oversized chunk"
            )
        if eof:
            detached = _detach_bridge(registry, bridge.bridge_id)
            if detached is not None:
                await asyncio.to_thread(detached.process.close_sync)
        return {
            "bridge_id": bridge.bridge_id,
            "data_b64": base64.b64encode(data).decode("ascii"),
            "bytes": len(data),
            "eof": bool(eof),
        }
    finally:
        registry.end_operation(operation)


async def write_terminal_bridge_execute(
    bridge_id: str,
    data_b64: str,
) -> dict[str, Any]:
    """Write one bounded raw byte chunk to a terminal bridge in order."""
    registry = _bridge_registry()
    operation = registry.begin_operation()
    try:
        data = _decode_chunk(data_b64)
        bridge = _lookup_bridge(registry, bridge_id)
        await asyncio.to_thread(bridge.process.write_all, data)
        return {
            "bridge_id": bridge.bridge_id,
            "written_bytes": len(data),
        }
    finally:
        registry.end_operation(operation)


async def resize_terminal_bridge_execute(
    bridge_id: str,
    cols: int,
    rows: int,
) -> dict[str, Any]:
    """Resize the PTY used by one raw terminal attachment."""
    registry = _bridge_registry()
    operation = registry.begin_operation()
    try:
        columns, lines = _terminal_size(cols, rows)
        bridge = _lookup_bridge(registry, bridge_id)
        resized = await asyncio.to_thread(
            bridge.process.resize,
            columns,
            lines,
        )
        with registry.lock:
            current = registry.bridges.get(bridge.bridge_id)
            if current is not None:
                current.cols = columns
                current.rows = lines
                current.last_activity = time.monotonic()
        return {
            "bridge_id": bridge.bridge_id,
            "cols": columns,
            "rows": lines,
            "resized": bool(resized),
            "backend": bridge.process.backend,
        }
    finally:
        registry.end_operation(operation)


async def close_terminal_bridge_execute(bridge_id: str) -> dict[str, Any]:
    """Close a raw client without terminating its persistent shell."""
    registry = _bridge_registry()
    operation = registry.begin_operation()
    try:
        normalized = _bridge_id(bridge_id)
        bridge = _detach_bridge(registry, normalized)
        if bridge is None:
            return {"bridge_id": normalized, "closed": False}
        await asyncio.to_thread(bridge.process.close_sync)
        return {"bridge_id": normalized, "closed": True}
    finally:
        registry.end_operation(operation)
