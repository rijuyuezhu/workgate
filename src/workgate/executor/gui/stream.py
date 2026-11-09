"""Outbound authenticated native Desktop stream; never a per-frame tool RPC."""

import asyncio
import base64
import contextlib
import io
import json
import os
import re
import time
from dataclasses import dataclass
from typing import Any, cast
from urllib.parse import urlsplit, urlunsplit

from mcp.types import ImageContent
from PIL import Image
from pydantic import TypeAdapter
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import SecurityError

from ...protocol.desktop import (
    DESKTOP_CAPTURE_INTERVAL_S,
    DESKTOP_MAX_CONTROL_BYTES,
    DESKTOP_MAX_FRAME_BYTES,
    DESKTOP_MAX_HEIGHT,
    DESKTOP_MAX_WIDTH,
    EXECUTOR_DESKTOP_STREAM_ROUTE,
)
from ...schemas.input_models.gui import GuiActionsArg
from ..profile import ExecutorProfile
from ..tool_session.lifecycle import session_lifecycle_lock
from .base import GuiService

_STREAM_ID = re.compile(r"^stream_[A-Za-z0-9_-]{20,128}$")
_ACTIONS = TypeAdapter(GuiActionsArg)


class _NoRedirectConnect(connect):
    def process_redirect(self, exc: Exception) -> Exception | str:
        target = super().process_redirect(exc)
        if isinstance(target, str):
            return SecurityError(
                "Desktop stream cannot redirect Executor credentials"
            )
        return target


def _url(control_url: str, stream_id: str) -> str:
    parsed = urlsplit(control_url)
    return urlunsplit(
        (
            "wss" if parsed.scheme == "https" else "ws",
            parsed.netloc,
            EXECUTOR_DESKTOP_STREAM_ROUTE.format(stream_id=stream_id),
            "",
            "",
        )
    )


def compress_desktop_frame(image_data: bytes) -> bytes:
    """Decode bounded GUI image and fit JPEG into the one-frame wire budget."""
    with Image.open(io.BytesIO(image_data)) as source:
        if source.width > 16_384 or source.height > 16_384:
            raise ValueError(
                "Source Desktop image dimensions exceed native limit"
            )
        image = source.convert("RGB")
    image.thumbnail(
        (DESKTOP_MAX_WIDTH, DESKTOP_MAX_HEIGHT), Image.Resampling.LANCZOS
    )
    for scale in (1.0, 0.75, 0.5):
        target = image if scale == 1.0 else image.copy()
        if scale != 1.0:
            target.thumbnail(
                (
                    max(1, int(image.width * scale)),
                    max(1, int(image.height * scale)),
                ),
                Image.Resampling.LANCZOS,
            )
        for quality in (75, 60, 45, 30):
            output = io.BytesIO()
            target.save(output, format="JPEG", quality=quality, optimize=True)
            payload = output.getvalue()
            if len(payload) <= DESKTOP_MAX_FRAME_BYTES:
                return payload
    raise ValueError("Desktop frame cannot fit the 128KiB wire limit")


@dataclass
class ExecutorDesktopStream:
    """One session/window-bound outbound Desktop socket and serial capture loop."""

    profile: ExecutorProfile
    stream_id: str
    session_id: str
    window_id: str
    gui: GuiService
    websocket: ClientConnection

    async def _send(self, payload: dict) -> None:
        encoded = json.dumps(
            payload, separators=(", ", ":"), ensure_ascii=False
        )
        if len(encoded.encode("utf-8")) > DESKTOP_MAX_CONTROL_BYTES:
            raise ValueError("Desktop message exceeds protocol bound")
        await self.websocket.send(encoded)

    async def _frame(self, previous_image: bytes) -> tuple[bytes, str]:
        async with session_lifecycle_lock(self.session_id):
            observed = await self.gui.snapshot(
                self.session_id,
                self.window_id,
                screenshot=True,
                include_elements=False,
                max_elements=1,
                max_depth=1,
            )
        meta = observed.structured_content
        content = next(
            (c for c in observed.content if isinstance(c, ImageContent)), None
        )
        if not isinstance(meta, dict) or content is None:
            raise RuntimeError("Desktop frame is unavailable")
        raw = base64.b64decode(content.data, validate=True)
        jpeg = await asyncio.to_thread(compress_desktop_frame, raw)
        window = meta.get("window")
        if (
            not isinstance(window, dict)
            or str(window.get("id")) != self.window_id
        ):
            raise ValueError("Desktop window identity changed")
        bounds = window.get("bounds")
        if not isinstance(bounds, dict):
            raise ValueError("Desktop window geometry unavailable")
        geometry = {
            axis: bounds.get(axis) for axis in ("width", "height", "x", "y")
        }
        width, height = geometry["width"], geometry["height"]
        if (
            not isinstance(width, int)
            or width <= 0
            or not isinstance(height, int)
            or height <= 0
        ):
            raise ValueError("Desktop window geometry invalid")
        state_id = str(meta.get("state_id") or "")
        if not re.fullmatch(r"[0-9a-f]{32}", state_id):
            raise ValueError("Desktop observation is missing state proof")
        data = {
            "type": "frame" if jpeg != previous_image else "lease",
            "state_id": state_id,
            "state_ttl_s": float(meta.get("state_ttl_s") or 0),
            "window": {"bounds": geometry},
            "capabilities": {
                "coordinate_input": meta.get("capabilities", {}).get(
                    "coordinate_input"
                )
                is True
                if isinstance(meta.get("capabilities"), dict)
                else False,
            },
        }
        await self._send(data)
        if jpeg != previous_image:
            # One in-flight frame; await applies WebSocket transport backpressure.
            await self.websocket.send(jpeg)
        return jpeg, state_id

    async def run(self) -> None:
        last_frame = b""
        awaiting_ack = False
        latest_state_id = ""
        active = False
        paused = False
        force_capture = False
        # Prevent an Executor from capturing or queueing frames before browser attach.
        focus_sensitive = (
            os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland"
        )
        last_capture = 0.0
        try:
            await self._send({"type": "ready", "mode": "desktop-jpeg"})
            while True:
                delay = (
                    max(
                        0.0,
                        DESKTOP_CAPTURE_INTERVAL_S
                        - (time.monotonic() - last_capture),
                    )
                    if active
                    and not paused
                    and (force_capture or not focus_sensitive)
                    and not awaiting_ack
                    else 15.0
                )
                message = None
                with contextlib.suppress(TimeoutError):
                    message = await asyncio.wait_for(
                        self.websocket.recv(), timeout=delay
                    )
                if isinstance(message, bytes):
                    raise ValueError("Desktop control input must be JSON")
                if isinstance(message, str):
                    if len(message.encode("utf-8")) > DESKTOP_MAX_CONTROL_BYTES:
                        raise ValueError("Desktop control input is oversized")
                    command = json.loads(message)
                    if not isinstance(command, dict):
                        raise ValueError("Desktop control input is malformed")
                    kind = command.get("type")
                    if kind == "ack":
                        awaiting_ack = False
                    elif kind == "start":
                        active, paused, force_capture = True, False, True
                    elif kind == "pause":
                        paused = True
                    elif kind in {"resume", "refresh"}:
                        if active:
                            paused, force_capture = False, True
                    elif kind == "action":
                        try:
                            if not active or paused or not latest_state_id:
                                raise ValueError(
                                    "Desktop action requires a fresh observation"
                                )
                            if command.get("state_id") != latest_state_id:
                                raise ValueError(
                                    "Desktop observation was superseded"
                                )
                            action = command.get("action")
                            if not isinstance(action, dict):
                                raise ValueError(
                                    "Desktop action must be an object"
                                )
                            actions = _ACTIONS.validate_python([action])
                            latest_state_id = (
                                ""  # One-use proof even on failed input.
                            )
                            async with session_lifecycle_lock(self.session_id):
                                await self.gui.act(
                                    self.session_id,
                                    self.window_id,
                                    str(command["state_id"]),
                                    cast(
                                        list[dict[str, Any]],
                                        [dict(item) for item in actions],
                                    ),
                                )
                        except Exception:
                            # Drop input failures without ending the viewing session;
                            # never echo native input or window details to the browser.
                            await self._send(
                                {"type": "action-result", "ok": False}
                            )
                        else:
                            await self._send(
                                {"type": "action-result", "ok": True}
                            )
                        force_capture = True
                    elif kind != "stop":
                        raise ValueError("Unknown Desktop control type")
                    else:
                        return
                if awaiting_ack:
                    # A frame's metadata and JPEG are one outstanding unit.
                    # Never capture another large image until browser ACK.
                    continue
                if not active or paused:
                    if active and paused and message is None:
                        await self._send({"type": "heartbeat"})
                    continue
                if not force_capture and focus_sensitive:
                    if message is None:
                        await self._send({"type": "heartbeat"})
                    continue
                if time.monotonic() - last_capture < DESKTOP_CAPTURE_INTERVAL_S:
                    continue
                force_capture = False
                previous_frame = last_frame
                last_frame, latest_state_id = await self._frame(last_frame)
                awaiting_ack = last_frame != previous_frame
                # Bound the *end-to-end* frame rate; a slow capture must not
                # create a zero-timeout loop that starves Stop/pause input.
                last_capture = time.monotonic()
        except asyncio.CancelledError:
            raise
        except Exception:
            # Never reflect native window titles or control authentication in errors.
            with contextlib.suppress(Exception):
                await self._send(
                    {
                        "type": "error",
                        "message": "Desktop stream failed; reconnect",
                    }
                )
        finally:
            with contextlib.suppress(Exception):
                await self.websocket.close()


async def connect_executor_desktop_stream(
    profile: ExecutorProfile,
    gui: GuiService,
    *,
    stream_id: str,
    session_id: str,
    window_id: str,
) -> ExecutorDesktopStream:
    if not _STREAM_ID.fullmatch(stream_id):
        raise ValueError("Invalid Desktop stream id")
    if not session_id or not window_id:
        raise ValueError("Desktop stream requires session and window")
    socket = await _NoRedirectConnect(
        _url(profile.control_url, stream_id),
        additional_headers={"Authorization": f"Bearer {profile.credential}"},
        compression=None,
        max_size=DESKTOP_MAX_CONTROL_BYTES,
        max_queue=1,
        write_limit=DESKTOP_MAX_FRAME_BYTES,
        proxy=True if profile.control_url.startswith("https://") else None,
    )
    try:
        accepted = await asyncio.wait_for(socket.recv(), timeout=10)
        handshake = json.loads(accepted) if isinstance(accepted, str) else None
        if (
            not isinstance(handshake, dict)
            or handshake.get("type") != "stream-accepted"
            or handshake.get("stream_id") != stream_id
        ):
            raise RuntimeError("Control rejected Desktop stream")
    except BaseException:
        await socket.close()
        raise
    return ExecutorDesktopStream(
        profile, stream_id, session_id, window_id, gui, socket
    )
