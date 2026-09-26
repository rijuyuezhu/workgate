"""Outbound executor WebSocket client for ephemeral terminal byte streams."""

import asyncio
import base64
import contextlib
import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import SecurityError

from ...protocol.terminal import (
    EXECUTOR_TERMINAL_STREAM_ROUTE,
    PERSISTENT_SHELL_MAX_COLUMNS,
    PERSISTENT_SHELL_MAX_ROWS,
    PERSISTENT_SHELL_MIN_COLUMNS,
    PERSISTENT_SHELL_MIN_ROWS,
    TERMINAL_STREAM_MAX_FRAME_BYTES,
)
from ..profile import ExecutorProfile
from .bridge import (
    close_terminal_bridge_execute,
    open_terminal_bridge_execute,
    read_terminal_bridge_execute,
    resize_terminal_bridge_execute,
    write_terminal_bridge_execute,
)

_STREAM_READ_WAIT_MS = 100
_STREAM_HANDSHAKE_TIMEOUT_S = 10.0
_STREAM_ID_PATTERN = re.compile(r"^stream_[A-Za-z0-9_-]{20,128}$")


class _NoRedirectConnect(connect):
    """WebSocket connector that never forwards executor credentials by redirect."""

    def process_redirect(self, exc: Exception) -> Exception | str:
        result = super().process_redirect(exc)
        if isinstance(result, str):
            return SecurityError(
                "terminal stream WebSocket redirects are disabled"
            )
        return result


def _stream_url(control_url: str, stream_id: str) -> str:
    parsed = urlsplit(control_url)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    path = EXECUTOR_TERMINAL_STREAM_ROUTE.format(stream_id=stream_id)
    return urlunsplit((scheme, parsed.netloc, path, "", ""))


def _bounded_dimension(
    value: Any,
    *,
    minimum: int,
    maximum: int,
    label: str,
) -> int:
    try:
        normalized = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be an integer") from exc
    if not minimum <= normalized <= maximum:
        raise ValueError(f"{label} must be between {minimum} and {maximum}")
    return normalized


@dataclass
class ExecutorTerminalStream:
    """One connected outbound stream bound to an executor-local raw bridge."""

    profile: ExecutorProfile
    stream_id: str
    shell_id: str
    bridge_id: str
    backend: str
    websocket: ClientConnection

    async def run(self) -> None:
        """Relay raw PTY bytes until either endpoint or the bridge closes."""
        tasks: list[asyncio.Task[None]] = []
        try:
            await self._send_json(
                {
                    "type": "ready",
                    "executor_id": str(self.profile.executor_id),
                    "shell_id": self.shell_id,
                    "mode": "pty",
                    "backend": self.backend,
                }
            )
            tasks = [
                asyncio.create_task(self._bridge_to_control()),
                asyncio.create_task(self._control_to_bridge()),
            ]
            done, pending = await asyncio.wait(
                tasks, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                task.result()
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            with contextlib.suppress(Exception):
                await self.websocket.close()
            with contextlib.suppress(Exception):
                await close_terminal_bridge_execute(self.bridge_id)

    async def _bridge_to_control(self) -> None:
        while True:
            result = await read_terminal_bridge_execute(
                self.bridge_id,
                TERMINAL_STREAM_MAX_FRAME_BYTES,
                _STREAM_READ_WAIT_MS,
            )
            encoded = str(result.get("data_b64") or "")
            if encoded:
                data = base64.b64decode(encoded, validate=True)
                if len(data) > TERMINAL_STREAM_MAX_FRAME_BYTES:
                    raise RuntimeError(
                        "Terminal bridge returned an oversized frame"
                    )
                if data:
                    await self.websocket.send(data)
            if bool(result.get("eof")):
                await self._send_json(
                    {
                        "type": "exit",
                        "executor_id": str(self.profile.executor_id),
                        "shell_id": self.shell_id,
                        "message": "Raw terminal client exited",
                        "mode": "pty",
                    }
                )
                return

    async def _control_to_bridge(self) -> None:
        async for message in self.websocket:
            if isinstance(message, bytes):
                if len(message) > TERMINAL_STREAM_MAX_FRAME_BYTES:
                    raise ValueError("Terminal input is too large")
                await write_terminal_bridge_execute(
                    self.bridge_id,
                    base64.b64encode(message).decode("ascii"),
                )
                continue
            await self._handle_control(message)

    async def _handle_control(self, payload: str) -> None:
        if len(payload.encode("utf-8")) > TERMINAL_STREAM_MAX_FRAME_BYTES:
            raise ValueError("Terminal control frame is too large")
        try:
            control = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ValueError(
                "Terminal control frame must be valid JSON"
            ) from exc
        if not isinstance(control, dict):
            raise ValueError("Terminal control frame must be an object")
        kind = str(control.get("type") or "")
        if kind == "resize":
            cols = _bounded_dimension(
                control.get("cols"),
                minimum=PERSISTENT_SHELL_MIN_COLUMNS,
                maximum=PERSISTENT_SHELL_MAX_COLUMNS,
                label="cols",
            )
            rows = _bounded_dimension(
                control.get("rows"),
                minimum=PERSISTENT_SHELL_MIN_ROWS,
                maximum=PERSISTENT_SHELL_MAX_ROWS,
                label="rows",
            )
            await resize_terminal_bridge_execute(
                self.bridge_id,
                cols,
                rows,
            )
            return
        if kind == "input":
            value = str(control.get("data") or "")
            raw = value.encode("utf-8")
            if bool(control.get("enter")):
                raw += b"\r"
            if len(raw) > TERMINAL_STREAM_MAX_FRAME_BYTES:
                raise ValueError("Terminal input is too large")
            await write_terminal_bridge_execute(
                self.bridge_id,
                base64.b64encode(raw).decode("ascii"),
            )
            return
        if kind == "ping":
            await self._send_json(
                {
                    "type": "pong",
                    "executor_id": str(self.profile.executor_id),
                    "shell_id": self.shell_id,
                    "mode": "pty",
                }
            )
            return
        if kind == "close":
            await self.websocket.close()
            return
        raise ValueError("Unsupported terminal control frame")

    async def _send_json(self, payload: dict[str, Any]) -> None:
        await self.websocket.send(
            json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        )


async def connect_executor_terminal_stream(
    profile: ExecutorProfile,
    *,
    stream_id: str,
    shell_id: str,
    cols: int,
    rows: int,
) -> ExecutorTerminalStream:
    """Open the local raw bridge and establish the outbound control WebSocket."""
    if not _STREAM_ID_PATTERN.fullmatch(str(stream_id)):
        raise ValueError("Invalid terminal stream_id")
    bridge = await open_terminal_bridge_execute(shell_id, cols, rows)
    bridge_id = str(bridge["bridge_id"])
    websocket: ClientConnection | None = None
    try:
        websocket = await _NoRedirectConnect(
            _stream_url(profile.control_url, stream_id),
            additional_headers={
                "Authorization": f"Bearer {profile.credential}",
            },
            compression=None,
            max_size=TERMINAL_STREAM_MAX_FRAME_BYTES,
            max_queue=1,
            write_limit=TERMINAL_STREAM_MAX_FRAME_BYTES,
            proxy=True if profile.control_url.startswith("https://") else None,
        )
        accepted = await asyncio.wait_for(
            websocket.recv(), timeout=_STREAM_HANDSHAKE_TIMEOUT_S
        )
        if not isinstance(accepted, str):
            raise RuntimeError(
                "control returned a non-text terminal stream handshake"
            )
        try:
            handshake = json.loads(accepted)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                "control returned an invalid terminal stream handshake"
            ) from exc
        if (
            not isinstance(handshake, dict)
            or handshake.get("type") != "stream-accepted"
            or handshake.get("stream_id") != stream_id
        ):
            raise RuntimeError("control rejected terminal stream handshake")
    except BaseException:
        if websocket is not None:
            with contextlib.suppress(Exception):
                await websocket.close()
        with contextlib.suppress(Exception):
            await close_terminal_bridge_execute(bridge_id)
        raise
    return ExecutorTerminalStream(
        profile=profile,
        stream_id=stream_id,
        shell_id=str(bridge["shell_id"]),
        bridge_id=bridge_id,
        backend=str(bridge["backend"]),
        websocket=websocket,
    )
