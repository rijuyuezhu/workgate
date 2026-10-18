"""POSIX PTY attachment to a persistent tmux session."""

import contextlib
import errno
import os
import select
import signal
import subprocess
import threading

from ...protocol.terminal import (
    TERMINAL_BRIDGE_BACKEND,
    TerminalBridgeNotFoundError,
)
from .tmux import detached_tmux_env, require_tmux


class UnixTmuxAttach:
    """One POSIX PTY hosting a tmux attach client."""

    backend = TERMINAL_BRIDGE_BACKEND

    def __init__(self, shell_id: str, cols: int, rows: int) -> None:
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
