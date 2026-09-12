"""WebSocket lifecycle orchestration for Human UI terminals."""

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from starlette.types import Message
from starlette.websockets import WebSocket, WebSocketDisconnect

from ...protocol.terminal import (
    TerminalBridgeBusyError,
    TerminalBridgeNotFoundError,
    TerminalBridgeUnsupportedError,
)
from .terminal_protocol import (
    UI_TERMINAL_EXECUTOR_POLL_INTERVAL_S,
    UI_TERMINAL_METADATA_MAX_BYTES,
    UI_TERMINAL_SUBPROTOCOL,
    TerminalCloseControl,
    TerminalInputControl,
    TerminalPingControl,
    TerminalResizeControl,
    TerminalWebSocketRequest,
    _TerminalBridgeHandle,
    _websocket_protocols,
    parse_terminal_binary_input,
    parse_terminal_control,
)

AsyncCall = Callable[..., Awaitable[Any]]


@dataclass(frozen=True)
class TerminalWebSocketBackend:
    """Executor adapter callbacks used by the transport lifecycle."""

    list_shells: AsyncCall
    """List available persistent shells for the selected executor_id."""
    read_shell: AsyncCall
    """Read a bounded snapshot from a persistent shell."""
    send_shell: AsyncCall
    """Send textual input to a persistent shell."""
    resize_shell: AsyncCall
    """Resize a persistent shell backend."""
    open_bridge: AsyncCall
    """Open an exclusive raw terminal bridge when supported."""
    read_bridge: AsyncCall
    """Read one bounded chunk from an open raw bridge."""
    write_bridge: AsyncCall
    """Write raw bytes to an open bridge."""
    resize_bridge: AsyncCall
    """Resize an open raw bridge."""
    close_bridge: AsyncCall
    """Close and release an open raw bridge."""


class _TerminalWebSocketConnection:
    def __init__(
        self,
        websocket: WebSocket,
        request: TerminalWebSocketRequest,
        *,
        backend: TerminalWebSocketBackend,
        bridge: _TerminalBridgeHandle | None,
        active_mode: str,
        marker: int,
        release_connection: Callable[[int], None],
        idle_timeout_s: int,
        audit_event: Callable[..., None],
    ) -> None:
        self.websocket = websocket
        self.request = request
        self.backend = backend
        self.bridge = bridge
        self.active_mode = active_mode
        self.marker = marker
        self.release_connection = release_connection
        self.idle_timeout_s = max(0, idle_timeout_s)
        self.audit_event = audit_event
        self.current_cols = request.cols
        self.current_rows = request.rows
        self.send_lock = asyncio.Lock()
        self.last_activity = time.monotonic()
        self.cleaned_up = False

    async def _cleanup(self) -> None:
        if self.cleaned_up:
            return
        self.cleaned_up = True
        if self.bridge is not None:
            with contextlib.suppress(Exception):
                await self.backend.close_bridge(self.bridge)
        self.release_connection(self.marker)
        self.audit_event(
            "ui_terminal_disconnected",
            executor_id=self.request.executor_id,
            shell_id=self.request.shell_id,
            mode=self.active_mode,
        )
        with contextlib.suppress(Exception):
            await self.websocket.close()

    async def _send_json(self, payload: dict[str, Any]) -> None:
        async with self.send_lock:
            await self.websocket.send_json(payload)

    async def _send_bytes(self, payload: bytes) -> None:
        async with self.send_lock:
            await self.websocket.send_bytes(payload)

    def _touch(self) -> None:
        self.last_activity = time.monotonic()

    async def _send_exit(self, exc: Exception) -> None:
        payload: dict[str, Any] = {
            "type": "exit",
            "executor_id": self.request.executor_id,
            "shell_id": self.request.shell_id,
            "message": str(exc)[:UI_TERMINAL_METADATA_MAX_BYTES],
        }
        if self.request.announce_mode:
            payload["mode"] = self.active_mode
        await self._send_json(payload)

    async def _send_ready(self) -> None:
        if not self.request.announce_mode:
            return
        await self._send_json(
            {
                "type": "ready",
                "executor_id": self.request.executor_id,
                "shell_id": self.request.shell_id,
                "mode": self.active_mode,
                "backend": (
                    self.bridge.backend
                    if self.bridge is not None
                    else "tmux-snapshot"
                ),
            }
        )

    async def _snapshot_sender(self) -> None:
        previous: str | None = None
        interval = UI_TERMINAL_EXECUTOR_POLL_INTERVAL_S
        while True:
            try:
                result = await self.backend.read_shell(
                    self.request.executor_id,
                    self.request.shell_id,
                    self.request.lines,
                )
            except Exception as exc:
                await self._send_exit(exc)
                return
            output = str(result["output"])
            if output != previous:
                previous = output
                self._touch()
                await self._send_json(
                    {
                        "type": "snapshot",
                        "executor_id": self.request.executor_id,
                        "shell_id": self.request.shell_id,
                        "output": output,
                    }
                )
            await asyncio.sleep(interval)

    async def _pty_sender(self) -> None:
        if self.bridge is None:
            raise RuntimeError("Raw terminal bridge was not initialized")
        while True:
            try:
                data, eof = await self.backend.read_bridge(self.bridge)
            except Exception as exc:
                await self._send_exit(exc)
                return
            if data:
                self._touch()
                await self._send_bytes(data)
            if eof:
                await self._send_exit(
                    TerminalBridgeNotFoundError("Raw terminal client exited")
                )
                return

    async def _receive_message(self) -> Message:
        if not self.idle_timeout_s:
            return await self.websocket.receive()
        remaining = max(
            0.0,
            self.idle_timeout_s - (time.monotonic() - self.last_activity),
        )
        if remaining <= 0:
            raise TimeoutError
        return await asyncio.wait_for(
            self.websocket.receive(), timeout=remaining
        )

    async def _close_bad_request(self, reason: str) -> None:
        await self.websocket.close(code=4400, reason=reason[:120])

    async def _handle_binary(self, value: Any) -> bool:
        try:
            raw = parse_terminal_binary_input(value)
        except ValueError as exc:
            await self._close_bad_request(str(exc))
            return False
        if self.active_mode == "pty":
            if self.bridge is None:
                raise RuntimeError("Raw terminal bridge was not initialized")
            await self.backend.write_bridge(self.bridge, raw)
        else:
            await self.backend.send_shell(
                self.request.executor_id,
                self.request.shell_id,
                raw.decode("utf-8", errors="replace"),
                False,
            )
        return True

    async def _handle_control(self, text: str) -> bool:
        try:
            control = parse_terminal_control(
                text,
                active_mode=self.active_mode,
                current_cols=self.current_cols,
                current_rows=self.current_rows,
            )
        except ValueError as exc:
            await self._close_bad_request(str(exc))
            return False

        if isinstance(control, TerminalInputControl):
            if self.active_mode == "pty":
                if self.bridge is None:
                    raise RuntimeError(
                        "Raw terminal bridge was not initialized"
                    )
                await self.backend.write_bridge(self.bridge, control.raw_input)
            else:
                await self.backend.send_shell(
                    self.request.executor_id,
                    self.request.shell_id,
                    control.input_text,
                    control.enter,
                )
            return True

        if isinstance(control, TerminalResizeControl):
            self.current_cols = control.cols
            self.current_rows = control.rows
            if self.active_mode == "pty":
                if self.bridge is None:
                    raise RuntimeError(
                        "Raw terminal bridge was not initialized"
                    )
                await self.backend.resize_bridge(
                    self.bridge,
                    self.current_cols,
                    self.current_rows,
                )
            else:
                await self.backend.resize_shell(
                    self.request.executor_id,
                    self.request.shell_id,
                    self.current_cols,
                    self.current_rows,
                )
            return True

        if isinstance(control, TerminalPingControl):
            payload: dict[str, Any] = {
                "type": "pong",
                "executor_id": self.request.executor_id,
                "shell_id": self.request.shell_id,
            }
            if self.request.announce_mode:
                payload["mode"] = self.active_mode
            await self._send_json(payload)
            return True

        if isinstance(control, TerminalCloseControl):
            return False
        raise RuntimeError("Unhandled terminal control")

    async def _receiver(self) -> None:
        try:
            while True:
                try:
                    message = await self._receive_message()
                except TimeoutError:
                    if (
                        self.idle_timeout_s
                        and time.monotonic() - self.last_activity
                        < self.idle_timeout_s
                    ):
                        continue
                    await self.websocket.close(
                        code=4408,
                        reason="Terminal connection idle timeout",
                    )
                    return
                if message["type"] == "websocket.disconnect":
                    return
                self._touch()

                if message.get("bytes") is not None:
                    if not await self._handle_binary(message["bytes"]):
                        return
                    continue

                text = message.get("text")
                if not text:
                    continue
                if not await self._handle_control(text):
                    return
        except (
            ConnectionError,
            RuntimeError,
            TerminalBridgeNotFoundError,
            ValueError,
        ) as exc:
            await self._send_exit(exc)

    async def run(self) -> None:
        self.audit_event(
            "ui_terminal_connected",
            executor_id=self.request.executor_id,
            shell_id=self.request.shell_id,
            requested_mode=self.request.requested_mode,
            mode=self.active_mode,
        )
        try:
            await self._send_ready()
        except Exception:
            await self._cleanup()
            return

        sender = (
            self._pty_sender
            if self.active_mode == "pty"
            else self._snapshot_sender
        )
        tasks = [
            asyncio.create_task(sender()),
            asyncio.create_task(self._receiver()),
        ]
        try:
            done, pending = await asyncio.wait(
                tasks,
                return_when=asyncio.FIRST_COMPLETED,
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
            await self._cleanup()


async def serve_terminal_websocket(
    websocket: WebSocket,
    request: TerminalWebSocketRequest,
    *,
    backend: TerminalWebSocketBackend,
    authorize: Callable[[WebSocket, str], tuple[bool, int, str]],
    reserve_connection: Callable[[], int | None],
    release_connection: Callable[[int], None],
    idle_timeout_s: int,
    audit_event: Callable[..., None],
) -> None:
    """Authorize, negotiate, and serve one terminal WebSocket connection."""

    authorized, close_code, reason = authorize(websocket, request.executor_id)
    if not authorized:
        await websocket.close(code=close_code, reason=reason[:120])
        return

    try:
        shells = await backend.list_shells(request.executor_id)
    except ConnectionError as exc:
        await websocket.close(code=1013, reason=str(exc)[:120])
        return
    except ValueError as exc:
        await websocket.close(code=4404, reason=str(exc)[:120])
        return
    except Exception:
        await websocket.close(
            code=1011,
            reason="Unable to inspect persistent shells",
        )
        return
    if request.shell_id not in {item["shell_id"] for item in shells["shells"]}:
        await websocket.close(code=4404, reason="Persistent shell not found")
        return

    marker = reserve_connection()
    if marker is None:
        await websocket.close(
            code=4429,
            reason="Too many Human UI terminal connections",
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
    except Exception:
        release_connection(marker)
        raise

    bridge: _TerminalBridgeHandle | None = None
    active_mode = "snapshot"
    if request.requested_mode in {"auto", "pty"}:
        try:
            bridge = await backend.open_bridge(
                request.executor_id,
                request.shell_id,
                request.cols,
                request.rows,
            )
            active_mode = "pty"
        except TerminalBridgeUnsupportedError as exc:
            if request.requested_mode == "pty":
                release_connection(marker)
                await websocket.close(code=4406, reason=str(exc)[:120])
                return
        except TerminalBridgeBusyError as exc:
            release_connection(marker)
            await websocket.close(code=4409, reason=str(exc)[:120])
            return
        except ConnectionError as exc:
            release_connection(marker)
            await websocket.close(code=1013, reason=str(exc)[:120])
            return
        except Exception as exc:
            release_connection(marker)
            await websocket.close(code=1011, reason=str(exc)[:120])
            return

    connection = _TerminalWebSocketConnection(
        websocket,
        request,
        backend=backend,
        bridge=bridge,
        active_mode=active_mode,
        marker=marker,
        release_connection=release_connection,
        idle_timeout_s=idle_timeout_s,
        audit_event=audit_event,
    )
    await connection.run()
