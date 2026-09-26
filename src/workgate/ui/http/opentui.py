"""Authenticated browser PTY bridge for the optional OpenTUI client."""

import asyncio
import contextlib
import importlib
import json
import logging
import os
import select
import signal
import subprocess
from typing import Any, Protocol

from starlette.websockets import WebSocket, WebSocketDisconnect

from ... import __version__
from ...config.control import ControlConfig
from ..runtime import resolve_tui_command
from ..security import (
    UI_API_PREFIX,
    UI_LOCAL_TOKEN_ENV,
    get_or_create_ui_local_token,
)
from .terminals import (
    UI_TERMINAL_SUBPROTOCOL,
    _authorize_websocket,
    _runtime,
    _websocket_protocols,
)

_LOGGER = logging.getLogger(__name__)
UI_OPENTUI_MIN_COLUMNS = 20
UI_OPENTUI_MAX_COLUMNS = 400
UI_OPENTUI_MIN_ROWS = 8
UI_OPENTUI_MAX_ROWS = 200


class OpenTuiProcess(Protocol):
    """Small async interface shared by Unix PTY and Windows ConPTY wrappers."""

    def resize(self, cols: int, rows: int) -> None:
        """Resize the attached terminal viewport."""
        ...

    async def read(self) -> bytes:
        """Read the next bounded output chunk, or empty bytes at EOF."""
        ...

    async def write(self, data: bytes) -> None:
        """Write one binary input chunk to the attached process."""
        ...

    async def exit_code(self) -> int | None:
        """Return the process exit code when it is available."""
        ...

    async def close(self) -> None:
        """Terminate the process and release terminal resources."""
        ...


def _bounded_int(
    value: Any,
    *,
    default: int,
    minimum: int,
    maximum: int,
    label: str,
) -> int:
    if value in {None, ""}:
        return default
    if isinstance(value, bool):
        raise ValueError(f"{label} must be an integer")
    try:
        normalized = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be an integer") from exc
    if not minimum <= normalized <= maximum:
        raise ValueError(f"{label} must be between {minimum} and {maximum}")
    return normalized


def _bounded_float(
    value: Any,
    *,
    default: float,
    minimum: float,
    maximum: float,
    label: str,
) -> float:
    if value in {None, ""}:
        return default
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a number")
    try:
        normalized = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a number") from exc
    if not minimum <= normalized <= maximum:
        raise ValueError(f"{label} must be between {minimum} and {maximum}")
    return normalized


class UnixOpenTuiProcess:
    """One OpenTUI subprocess attached to a Unix pseudo-terminal."""

    def __init__(
        self, command: list[str], env: dict[str, str], cols: int, rows: int
    ):
        import fcntl
        import pty
        import struct
        import termios

        self._fcntl = fcntl
        self._struct = struct
        self._termios = termios
        self.master_fd, slave_fd = pty.openpty()
        self.resize(cols, rows)
        self.process = subprocess.Popen(
            command,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            env=env,
            close_fds=True,
            start_new_session=True,
        )
        os.close(slave_fd)
        os.set_blocking(self.master_fd, False)

    def resize(self, cols: int, rows: int) -> None:
        """Resize the Unix PTY and notify the child process group."""
        winsize = self._struct.pack("HHHH", max(1, rows), max(1, cols), 0, 0)
        self._fcntl.ioctl(self.master_fd, self._termios.TIOCSWINSZ, winsize)
        process = getattr(self, "process", None)
        if process is not None and process.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGWINCH)

    async def read(self) -> bytes:
        """Read one nonblocking output chunk from the Unix PTY."""
        while True:
            try:
                return os.read(self.master_fd, 65_536)
            except BlockingIOError:
                await asyncio.sleep(0.01)
            except OSError:
                return b""

    def _write_all(self, data: bytes) -> None:
        remaining = memoryview(data)
        while remaining:
            try:
                written = os.write(self.master_fd, remaining)
            except BlockingIOError:
                select.select([], [self.master_fd], [], 0.1)
                continue
            if written <= 0:
                raise OSError("PTY write made no progress")
            remaining = remaining[written:]

    async def write(self, data: bytes) -> None:
        """Write all input bytes to the Unix PTY without truncation."""
        if data:
            await asyncio.to_thread(self._write_all, data)

    async def exit_code(self) -> int | None:
        """Return the Unix child exit code after a short bounded wait."""
        code = self.process.poll()
        if code is not None:
            return int(code)
        try:
            return int(
                await asyncio.wait_for(
                    asyncio.to_thread(self.process.wait), timeout=0.25
                )
            )
        except TimeoutError:
            return None

    async def close(self) -> None:
        """Close the Unix PTY and terminate the child process group."""
        with contextlib.suppress(OSError):
            os.close(self.master_fd)
        if self.process.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self.process.pid, signal.SIGTERM)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(
                    asyncio.to_thread(self.process.wait), timeout=2
                )
        if self.process.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self.process.pid, signal.SIGKILL)


class WindowsOpenTuiProcess:
    """One OpenTUI subprocess attached to a Windows ConPTY."""

    def __init__(
        self, command: list[str], env: dict[str, str], cols: int, rows: int
    ):
        try:
            PtyProcess = importlib.import_module("winpty").PtyProcess
        except (
            AttributeError,
            ImportError,
        ) as exc:  # pragma: no cover - Windows only.
            raise RuntimeError(
                "pywinpty is required for OpenTUI on Windows"
            ) from exc
        try:
            self.process = PtyProcess.spawn(
                command,
                dimensions=(max(1, rows), max(1, cols)),
                env=env,
            )
        except TypeError:
            self.process = PtyProcess.spawn(
                subprocess.list2cmdline(command),
                dimensions=(max(1, rows), max(1, cols)),
                env=env,
            )

    def resize(self, cols: int, rows: int) -> None:
        """Resize the Windows ConPTY viewport."""
        self.process.setwinsize(max(1, rows), max(1, cols))

    async def read(self) -> bytes:
        """Read one output chunk from the Windows ConPTY."""

        def read_chunk() -> Any:
            try:
                return self.process.read(65_536)
            except TypeError:
                return self.process.read()

        try:
            data = await asyncio.to_thread(read_chunk)
        except Exception:
            return b""
        return (
            data.encode("utf-8", errors="replace")
            if isinstance(data, str)
            else bytes(data)
        )

    async def write(self, data: bytes) -> None:
        """Decode and write one input chunk to the Windows ConPTY."""
        if data:
            await asyncio.to_thread(
                self.process.write, data.decode("utf-8", errors="replace")
            )

    async def exit_code(self) -> int | None:
        """Return the Windows child exit status after a bounded poll."""
        for _ in range(25):
            try:
                if not self.process.isalive():
                    value = getattr(self.process, "exitstatus", None)
                    return int(value) if value is not None else None
            except Exception:
                return None
            await asyncio.sleep(0.01)
        return None

    async def close(self) -> None:
        """Terminate the Windows ConPTY process tree."""
        with contextlib.suppress(Exception):
            await asyncio.to_thread(self.process.terminate, True)


def spawn_opentui_process(
    cols: int,
    rows: int,
    cell_aspect: float = 2.0,
    *,
    settings: ControlConfig,
) -> OpenTuiProcess:
    """Start OpenTUI with explicit control config and a private API credential."""
    env = os.environ.copy()
    env.update(
        {
            "TERM": "xterm-256color",
            "COLORTERM": "truecolor",
            "TERM_PROGRAM": "vscode",
            "TERM_PROGRAM_VERSION": f"workgate/{__version__}",
            "WORKGATE_UI_API_BASE": (
                f"http://127.0.0.1:{settings.port}{UI_API_PREFIX}"
            ),
            "WORKGATE_UI_MODE": "web",
            UI_LOCAL_TOKEN_ENV: get_or_create_ui_local_token(),
            "WORKGATE_UI_CELL_ASPECT": f"{cell_aspect:.4f}",
        }
    )
    command = resolve_tui_command(settings)
    if os.name == "nt":
        return WindowsOpenTuiProcess(command, env, cols, rows)
    return UnixOpenTuiProcess(command, env, cols, rows)


def _idle_remaining(last_activity: float, timeout: float, now: float) -> float:
    return max(0.0, timeout - max(0.0, now - last_activity))


async def ui_opentui_websocket(websocket: WebSocket) -> None:
    """Bridge one authenticated browser xterm instance to a private OpenTUI PTY."""
    try:
        cols = _bounded_int(
            websocket.query_params.get("cols"),
            default=120,
            minimum=UI_OPENTUI_MIN_COLUMNS,
            maximum=UI_OPENTUI_MAX_COLUMNS,
            label="cols",
        )
        rows = _bounded_int(
            websocket.query_params.get("rows"),
            default=36,
            minimum=UI_OPENTUI_MIN_ROWS,
            maximum=UI_OPENTUI_MAX_ROWS,
            label="rows",
        )
        cell_aspect = _bounded_float(
            websocket.query_params.get("cell_aspect"),
            default=2.0,
            minimum=0.5,
            maximum=5.0,
            label="cell_aspect",
        )
    except ValueError as exc:
        await websocket.close(code=4400, reason=str(exc)[:120])
        return

    runtime = _runtime(websocket)
    authorized, close_code, reason = _authorize_websocket(websocket, "local")
    if not authorized:
        await websocket.close(code=close_code, reason=reason[:120])
        return
    connections = runtime.human_ui_runtime.terminal_connections
    marker = connections.reserve(runtime.config.ui_terminal_max_connections)
    if marker is None:
        await websocket.close(
            code=4429, reason="Too many Human UI terminal connections"
        )
        return

    protocols = _websocket_protocols(websocket)
    subprotocol = (
        UI_TERMINAL_SUBPROTOCOL
        if UI_TERMINAL_SUBPROTOCOL in protocols
        else None
    )
    try:
        await websocket.accept(subprotocol=subprotocol)
        process = spawn_opentui_process(
            cols, rows, cell_aspect, settings=runtime.config
        )
    except Exception as exc:
        connections.release(marker)
        _LOGGER.exception("Unable to start OpenTUI")
        detail = f"{type(exc).__name__}: {exc}"
        with contextlib.suppress(Exception):
            await websocket.send_bytes(
                f"\r\nUnable to start OpenTUI: {detail}\r\n".encode()
            )
            await websocket.close(code=1011, reason=detail[:120])
        return

    settings = runtime.config
    loop = asyncio.get_running_loop()
    last_activity = loop.time()

    async def sender() -> None:
        nonlocal last_activity
        while True:
            data = await process.read()
            if not data:
                code = await process.exit_code()
                if code is not None:
                    close_code = 1000 if code == 0 else 1011
                    reason = (
                        "OpenTUI process exited"
                        if code == 0
                        else f"OpenTUI process exited with code {code}"
                    )
                    await websocket.close(code=close_code, reason=reason)
                return
            last_activity = loop.time()
            await websocket.send_bytes(data)

    async def receiver() -> None:
        nonlocal cols, rows, last_activity
        timeout = max(0, settings.ui_terminal_idle_timeout_s)
        while True:
            if timeout:
                remaining = _idle_remaining(last_activity, timeout, loop.time())
                if remaining <= 0:
                    await websocket.close(
                        code=4408, reason="OpenTUI idle timeout"
                    )
                    return
                try:
                    message = await asyncio.wait_for(
                        websocket.receive(), timeout=remaining
                    )
                except TimeoutError:
                    continue
            else:
                message = await websocket.receive()
            last_activity = loop.time()
            if message["type"] == "websocket.disconnect":
                return
            data = message.get("bytes")
            if data is not None:
                await process.write(data)
                continue
            text = message.get("text")
            if not text:
                continue
            try:
                control = json.loads(text)
            except json.JSONDecodeError:
                await process.write(text.encode())
                continue
            if not isinstance(control, dict) or control.get("type") != "resize":
                continue
            try:
                cols = _bounded_int(
                    control.get("cols"),
                    default=cols,
                    minimum=UI_OPENTUI_MIN_COLUMNS,
                    maximum=UI_OPENTUI_MAX_COLUMNS,
                    label="cols",
                )
                rows = _bounded_int(
                    control.get("rows"),
                    default=rows,
                    minimum=UI_OPENTUI_MIN_ROWS,
                    maximum=UI_OPENTUI_MAX_ROWS,
                    label="rows",
                )
            except ValueError as exc:
                await websocket.close(code=4400, reason=str(exc)[:120])
                return
            process.resize(cols, rows)

    tasks = [asyncio.create_task(sender()), asyncio.create_task(receiver())]
    try:
        done, pending = await asyncio.wait(
            tasks, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            with contextlib.suppress(WebSocketDisconnect, RuntimeError):
                task.result()
    except WebSocketDisconnect:
        pass
    finally:
        connections.release(marker)
        await process.close()
        with contextlib.suppress(Exception):
            await websocket.close()
