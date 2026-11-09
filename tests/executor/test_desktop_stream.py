"""Desktop JPEG compression and GUI-state-safe outbound stream."""

import asyncio
import io
import json
from typing import Any, cast

import pytest
from PIL import Image

from tests.executor.test_gui_service import service
from workgate.executor.gui.stream import (
    ExecutorDesktopStream,
    compress_desktop_frame,
)
from workgate.protocol.desktop import DESKTOP_MAX_FRAME_BYTES


class _LocalWebSocket:
    def __init__(self) -> None:
        self.incoming: asyncio.Queue[str] = asyncio.Queue()
        self.outgoing: asyncio.Queue[str | bytes] = asyncio.Queue()
        self.closed = False

    async def recv(self) -> str:
        return await self.incoming.get()

    async def send(self, data: str | bytes) -> None:
        await self.outgoing.put(data)

    async def close(self) -> None:
        self.closed = True

    async def message(self) -> str | bytes:
        return await asyncio.wait_for(self.outgoing.get(), timeout=3)


@pytest.mark.parametrize("size", [(40, 30), (3000, 1700), (1280, 800)])
def test_desktop_jpeg_compression_limits(size):
    source = io.BytesIO()
    Image.new("RGB", size, "#99bbdd").save(source, format="PNG")
    jpeg = compress_desktop_frame(source.getvalue())
    assert jpeg.startswith(b"\xff\xd8")
    assert len(jpeg) <= DESKTOP_MAX_FRAME_BYTES
    with Image.open(io.BytesIO(jpeg)) as image:
        assert image.width <= 1280
        assert image.height <= 800


@pytest.mark.asyncio
async def test_desktop_stream_frames_lease_and_one_use_actions(
    tmp_path, monkeypatch
):
    gui, backend, _store, session_id, _other_session = service(tmp_path)
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    ws = _LocalWebSocket()
    stream = ExecutorDesktopStream(
        cast(Any, None),
        "stream_testing",
        session_id,
        "window-1",
        gui,
        cast(Any, ws),
    )
    task = asyncio.create_task(stream.run())
    try:
        assert json.loads(cast(str, await ws.message()))["type"] == "ready"
        # Before explicit browser start a Wayland frame is never captured.
        assert not ws.outgoing.qsize()
        await ws.incoming.put('{"type":"start"}')
        metadata = json.loads(cast(str, await ws.message()))
        assert metadata["type"] == "frame"
        assert metadata["window"]["bounds"]["width"] == 40
        jpeg = await ws.message()
        assert isinstance(jpeg, bytes) and len(jpeg) <= DESKTOP_MAX_FRAME_BYTES
        old_state = metadata["state_id"]
        await ws.incoming.put('{"type":"ack"}')
        # No periodic Wayland captures; refresh is explicit.
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(ws.outgoing.get(), timeout=0.06)
        await ws.incoming.put('{"type":"refresh"}')
        renewed = json.loads(cast(str, await ws.message()))
        assert renewed["type"] == "lease"
        assert renewed["state_id"] != old_state
        await ws.incoming.put(
            json.dumps(
                {
                    "type": "action",
                    "state_id": renewed["state_id"],
                    "action": {"type": "focus"},
                }
            )
        )
        assert (
            json.loads(cast(str, await ws.message()))["type"] == "action-result"
        )
        assert backend.actions[-1][1]["type"] == "focus"
        assert json.loads(cast(str, await ws.message()))["type"] in {
            "lease",
            "frame",
        }
        await ws.incoming.put('{"type":"stop"}')
        await asyncio.wait_for(task, timeout=3)
        assert ws.closed
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_desktop_stream_rejects_reused_observation(tmp_path, monkeypatch):
    gui, backend, _, session_id, _ = service(tmp_path)
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    ws = _LocalWebSocket()
    stream = ExecutorDesktopStream(
        cast(Any, None),
        "stream_testing",
        session_id,
        "window-1",
        gui,
        cast(Any, ws),
    )
    task = asyncio.create_task(stream.run())
    try:
        await ws.message()
        await ws.incoming.put('{"type":"start"}')
        frame = json.loads(cast(str, await ws.message()))
        await ws.message()
        await ws.incoming.put('{"type":"ack"}')
        await ws.incoming.put('{"type":"refresh"}')
        await ws.message()
        await ws.incoming.put(
            json.dumps(
                {
                    "type": "action",
                    "state_id": frame["state_id"],
                    "action": {"type": "focus"},
                }
            )
        )
        failure = json.loads(cast(str, await ws.message()))
        assert failure == {"type": "action-result", "ok": False}
        next_frame = json.loads(cast(str, await ws.message()))
        assert next_frame["type"] in {"lease", "frame"}
        assert not backend.actions
        await ws.incoming.put('{"type":"stop"}')
        await asyncio.wait_for(task, timeout=3)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_desktop_stream_real_loopback_websockets(tmp_path, monkeypatch):
    """Actual browser/Executor WS relay to GuiService without per-frame RPC."""
    import socket
    from types import SimpleNamespace

    import uvicorn
    from starlette.applications import Starlette
    from websockets.asyncio.client import connect
    from websockets.typing import Subprotocol

    from tests.control.test_control_stream_routes import _Transport
    from workgate.control.http.stream_routes import desktop_stream_routes
    from workgate.control.streams import ControlStreamHub
    from workgate.executor.gui.stream import connect_executor_desktop_stream
    from workgate.protocol.desktop import (
        DESKTOP_BROWSER_SUBPROTOCOL,
        DESKTOP_BROWSER_TOKEN_PREFIX,
    )

    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    gui, backend, _store, session_id, _other = service(tmp_path)
    hub = ControlStreamHub(
        max_streams=1,
        idle_timeout_s=15,
        max_frame_bytes=DESKTOP_MAX_FRAME_BYTES,
    )
    app = Starlette(
        routes=desktop_stream_routes(
            cast(Any, _Transport({"executor-credential": "ex1"})), hub
        )
    )
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            log_level="error",
            access_log=False,
            lifespan="off",
        )
    )
    service_task = asyncio.create_task(server.serve())
    stream_task = None
    try:
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(0.02)
        assert server.started
        grant = await hub.create("ex1")
        profile = cast(
            Any,
            SimpleNamespace(
                control_url=f"http://127.0.0.1:{port}",
                credential="executor-credential",
                executor_id="ex1",
            ),
        )
        stream = await connect_executor_desktop_stream(
            profile,
            gui,
            stream_id=grant.stream_id,
            session_id=session_id,
            window_id="window-1",
        )
        stream_task = asyncio.create_task(stream.run())
        async with connect(
            f"ws://127.0.0.1:{port}/desktop-stream/{grant.stream_id}",
            subprotocols=[
                Subprotocol(DESKTOP_BROWSER_SUBPROTOCOL),
                Subprotocol(
                    f"{DESKTOP_BROWSER_TOKEN_PREFIX}{grant.browser_token}"
                ),
            ],
            compression=None,
        ) as browser:
            assert (
                json.loads(
                    cast(str, await asyncio.wait_for(browser.recv(), 2))
                )["type"]
                == "ready"
            )
            await browser.send('{"type":"start"}')
            meta = json.loads(
                cast(str, await asyncio.wait_for(browser.recv(), 2))
            )
            assert meta["type"] == "frame"
            frame = await asyncio.wait_for(browser.recv(), 2)
            assert isinstance(frame, bytes) and frame.startswith(b"\xff\xd8")
            assert len(frame) <= DESKTOP_MAX_FRAME_BYTES
            await browser.send('{"type":"ack"}')
            await browser.send(
                json.dumps(
                    {
                        "type": "action",
                        "state_id": meta["state_id"],
                        "action": {"type": "focus"},
                    }
                )
            )
            reply = json.loads(
                cast(str, await asyncio.wait_for(browser.recv(), 2))
            )
            assert reply == {"type": "action-result", "ok": True}
            assert backend.actions[-1][1]["type"] == "focus"
            renewed = json.loads(
                cast(str, await asyncio.wait_for(browser.recv(), 2))
            )
            assert renewed["type"] in {"frame", "lease"}
            if renewed["type"] == "frame":
                await browser.recv()
            await browser.send('{"type":"stop"}')
        await asyncio.wait_for(stream_task, 2)
        await asyncio.wait_for(hub.wait_closed(grant.stream_id), 2)
        assert hub.active_count() == 0
    finally:
        if stream_task is not None and not stream_task.done():
            stream_task.cancel()
            await asyncio.gather(stream_task, return_exceptions=True)
        await hub.aclose()
        server.should_exit = True
        await asyncio.wait_for(service_task, 5)


@pytest.mark.asyncio
async def test_desktop_slow_capture_keeps_stop_responsive(
    tmp_path, monkeypatch
):
    """Stop must be read even when one capture takes longer than frame interval."""
    gui, _, _, session_id, _ = service(tmp_path)
    monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
    original_snapshot = gui.snapshot

    async def slow_snapshot(*args, **kwargs):
        await asyncio.sleep(1.05)
        return await original_snapshot(*args, **kwargs)

    monkeypatch.setattr(gui, "snapshot", slow_snapshot)
    ws = _LocalWebSocket()
    stream = ExecutorDesktopStream(
        cast(Any, None),
        "stream_slow",
        session_id,
        "window-1",
        gui,
        cast(Any, ws),
    )
    task = asyncio.create_task(stream.run())
    try:
        await ws.message()
        await ws.incoming.put('{"type":"start"}')
        assert json.loads(cast(str, await ws.message()))["type"] == "frame"
        assert isinstance(await ws.message(), bytes)
        await ws.incoming.put('{"type":"stop"}')
        await asyncio.wait_for(task, timeout=0.6)
        assert ws.closed
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_desktop_frame_ack_limits_outstanding_images(
    tmp_path, monkeypatch
):
    """An explicit refresh cannot enqueue a second image before browser ACK."""
    gui, _, _, session_id, _ = service(tmp_path)
    monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
    ws = _LocalWebSocket()
    stream = ExecutorDesktopStream(
        cast(Any, None),
        "stream_ack",
        session_id,
        "window-1",
        gui,
        cast(Any, ws),
    )
    task = asyncio.create_task(stream.run())
    try:
        await ws.message()  # ready
        await ws.incoming.put('{"type":"start"}')
        assert json.loads(cast(str, await ws.message()))["type"] == "frame"
        assert isinstance(await ws.message(), bytes)
        await ws.incoming.put('{"type":"refresh"}')
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(ws.outgoing.get(), timeout=0.06)
        # The old image has no transport queue; only after ACK can we refresh.
        await ws.incoming.put('{"type":"ack"}')
        assert json.loads(cast(str, await ws.message()))["type"] == "lease"
        await ws.incoming.put('{"type":"stop"}')
        await asyncio.wait_for(task, timeout=2)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_desktop_refresh_respects_capture_rate_on_wayland(
    tmp_path, monkeypatch
):
    """Explicit refresh and immediate ACK cannot bypass the capture-rate cap."""
    import time

    import workgate.executor.gui.stream as desktop

    gui, _, _, session_id, _ = service(tmp_path)
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    monkeypatch.setattr(desktop, "DESKTOP_CAPTURE_INTERVAL_S", 0.35)
    times = []
    capture = gui.snapshot

    async def measured(*args, **kwargs):
        times.append(time.monotonic())
        return await capture(*args, **kwargs)

    monkeypatch.setattr(gui, "snapshot", measured)
    ws = _LocalWebSocket()
    stream = ExecutorDesktopStream(
        cast(Any, None),
        "stream_rate",
        session_id,
        "window-1",
        gui,
        cast(Any, ws),
    )
    task = asyncio.create_task(stream.run())
    try:
        await ws.message()
        await ws.incoming.put('{"type":"start"}')
        assert json.loads(cast(str, await ws.message()))["type"] == "frame"
        assert isinstance(await ws.message(), bytes)
        await ws.incoming.put('{"type":"ack"}')
        await ws.incoming.put('{"type":"refresh"}')
        assert json.loads(cast(str, await ws.message()))["type"] == "lease"
        assert len(times) == 2
        assert times[1] - times[0] >= 0.32
        await ws.incoming.put('{"type":"stop"}')
        await asyncio.wait_for(task, timeout=2)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid",
    [b"binary", "x" * 8193, "[]", "{", '{"type":"unknown"}'],
)
async def test_desktop_stream_rejects_malformed_or_unbounded_client_messages(
    tmp_path, monkeypatch, invalid
):
    gui, backend, _, session_id, _ = service(tmp_path)
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    ws = _LocalWebSocket()
    stream = ExecutorDesktopStream(
        cast(Any, None),
        "stream_invalid",
        session_id,
        "window-1",
        gui,
        cast(Any, ws),
    )
    task = asyncio.create_task(stream.run())
    try:
        await ws.message()
        await ws.incoming.put(cast(str, invalid))
        assert json.loads(cast(str, await ws.message()))["type"] == "error"
        await asyncio.wait_for(task, timeout=2)
        assert not backend.actions
        assert ws.closed
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_desktop_stream_stays_view_only_until_start_and_disallows_bad_actions(
    tmp_path, monkeypatch
):
    gui, backend, _, session_id, _ = service(tmp_path)
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    ws = _LocalWebSocket()
    stream = ExecutorDesktopStream(
        cast(Any, None),
        "stream_view_only",
        session_id,
        "window-1",
        gui,
        cast(Any, ws),
    )
    task = asyncio.create_task(stream.run())
    try:
        await ws.message()
        await ws.incoming.put(
            json.dumps(
                {
                    "type": "action",
                    "state_id": "a" * 32,
                    "action": {"type": "focus"},
                }
            )
        )
        assert json.loads(cast(str, await ws.message())) == {
            "type": "action-result",
            "ok": False,
        }
        await ws.incoming.put('{"type":"start"}')
        frame = json.loads(cast(str, await ws.message()))
        await ws.message()
        await ws.incoming.put('{"type":"ack"}')
        await ws.incoming.put(
            json.dumps(
                {"type": "action", "state_id": frame["state_id"], "action": []}
            )
        )
        assert json.loads(cast(str, await ws.message())) == {
            "type": "action-result",
            "ok": False,
        }
        assert not backend.actions
        await ws.incoming.put('{"type":"pause"}')
        await ws.incoming.put('{"type":"stop"}')
        await asyncio.wait_for(task, timeout=2)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_desktop_stream_rejects_oversize_server_message(tmp_path):
    gui, _, _, session_id, _ = service(tmp_path)
    ws = _LocalWebSocket()
    stream = ExecutorDesktopStream(
        cast(Any, None),
        "stream_output",
        session_id,
        "window-1",
        gui,
        cast(Any, ws),
    )
    with pytest.raises(ValueError, match="protocol bound"):
        await stream._send({"data": "x" * 9000})
    assert ws.outgoing.empty()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "damage",
    ["no-image", "bad-window", "no-bounds", "bad-size", "no-observation"],
)
async def test_desktop_frame_rejects_invalid_native_observation(
    tmp_path, monkeypatch, damage
):
    gui, _, _, session_id, _ = service(tmp_path)
    snapshot = gui.snapshot

    async def damaged(*args, **kwargs):
        observed = await snapshot(*args, **kwargs)
        if damage == "no-image":
            observed.content = []
        elif damage == "bad-window":
            observed.structured_content["window"]["id"] = "another-window"
        elif damage == "no-bounds":
            observed.structured_content["window"].pop("bounds")
        elif damage == "bad-size":
            observed.structured_content["window"]["bounds"]["width"] = -1
        else:
            observed.structured_content.pop("state_id")
        return observed

    monkeypatch.setattr(gui, "snapshot", damaged)
    ws = _LocalWebSocket()
    stream = ExecutorDesktopStream(
        cast(Any, None),
        "stream_bad_native",
        session_id,
        "window-1",
        gui,
        cast(Any, ws),
    )
    with pytest.raises((RuntimeError, ValueError)):
        await stream._frame(b"")
    assert ws.outgoing.empty()


def test_desktop_frame_rejects_unbounded_source_and_incompressible_frame(
    monkeypatch,
):
    import workgate.executor.gui.stream as desktop

    source = io.BytesIO()
    Image.new("RGB", (16_385, 1), "white").save(source, format="PNG")
    with pytest.raises(ValueError, match="dimensions"):
        compress_desktop_frame(source.getvalue())
    monkeypatch.setattr(desktop, "DESKTOP_MAX_FRAME_BYTES", 10)
    source = io.BytesIO()
    Image.new("RGB", (60, 40), "blue").save(source, format="PNG")
    with pytest.raises(ValueError, match="cannot fit"):
        compress_desktop_frame(source.getvalue())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        '{"type":"wrong"}',
        '{"type":"stream-accepted","stream_id":"wrong"}',
        b"binary",
    ],
)
async def test_desktop_executor_handshake_closes_on_invalid_control_ack(
    monkeypatch, failure
):
    from types import SimpleNamespace

    import workgate.executor.gui.stream as desktop

    ws = _LocalWebSocket()
    await ws.incoming.put(cast(str, failure))

    async def fake_connect(*_args, **_kwargs):
        return ws

    monkeypatch.setattr(desktop, "_NoRedirectConnect", fake_connect)
    profile = cast(
        Any,
        SimpleNamespace(
            control_url="https://control.example", credential="secret"
        ),
    )
    with pytest.raises(RuntimeError, match="rejected"):
        await desktop.connect_executor_desktop_stream(
            profile,
            cast(Any, None),
            stream_id="stream_abcdefghijklmnopqrstuvwxyz",
            session_id="sess_valid",
            window_id="window-1",
        )
    assert ws.closed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stream_id,session_id,window_id",
    [
        ("bad", "sess_valid", "window-1"),
        ("stream_abcdefghijklmnopqrstuvwxyz", "", "window-1"),
        ("stream_abcdefghijklmnopqrstuvwxyz", "sess_valid", ""),
    ],
)
async def test_desktop_executor_connector_rejects_invalid_identity(
    stream_id, session_id, window_id
):
    from types import SimpleNamespace

    from workgate.executor.gui.stream import connect_executor_desktop_stream

    profile = cast(
        Any,
        SimpleNamespace(
            control_url="https://control.example", credential="secret"
        ),
    )
    with pytest.raises(ValueError):
        await connect_executor_desktop_stream(
            profile,
            cast(Any, None),
            stream_id=stream_id,
            session_id=session_id,
            window_id=window_id,
        )
