import asyncio
import base64
import json
from typing import Any, cast

import pytest
from websockets.datastructures import Headers
from websockets.exceptions import InvalidStatus, SecurityError
from websockets.http11 import Response

from workgate.executor.profile import ExecutorProfile
from workgate.executor.terminal import stream as stream_module
from workgate.protocol.credentials import new_executor_credential
from workgate.protocol.ids import new_executor_id


class _FakeWebSocket:
    def __init__(self) -> None:
        self.incoming: asyncio.Queue[Any] = asyncio.Queue()
        self.sent: list[str | bytes] = []
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        value = await self.incoming.get()
        if value is StopAsyncIteration:
            raise StopAsyncIteration
        return value

    async def recv(self) -> str:
        return json.dumps(
            {
                "type": "stream-accepted",
                "stream_id": "stream_abcdefghijklmnopqrstuvwxyz",
            }
        )

    async def send(self, value: str | bytes) -> None:
        self.sent.append(value)

    async def close(self) -> None:
        self.closed = True


class _FailingReadyWebSocket(_FakeWebSocket):
    async def send(self, value: str | bytes) -> None:
        raise RuntimeError("stream closed before ready")


class _BadHandshakeWebSocket(_FakeWebSocket):
    async def recv(self) -> str:
        return '{"type":"rejected","stream_id":"stream_abcdefghijklmnopqrstuvwxyz"}'


def _profile() -> ExecutorProfile:
    return ExecutorProfile(
        control_url="https://control.example",
        executor_id=new_executor_id(),
        credential=new_executor_credential(),
    )


@pytest.mark.asyncio
async def test_executor_terminal_stream_connects_outbound_and_relays_raw_bridge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    websocket = _FakeWebSocket()
    connect_calls: list[tuple[str, dict[str, Any]]] = []
    writes: list[bytes] = []
    resizes: list[tuple[int, int]] = []
    closed_bridges: list[str] = []
    release_eof = asyncio.Event()
    read_calls = 0

    async def fake_connect(url: str, **kwargs: Any):
        connect_calls.append((url, kwargs))
        return websocket

    async def fake_open(shell_id: str, cols: int, rows: int):
        assert (shell_id, cols, rows) == ("shell-1", 100, 30)
        return {
            "bridge_id": "bridge-1",
            "shell_id": shell_id,
            "cols": cols,
            "rows": rows,
            "backend": "fake-pty",
        }

    async def fake_read(bridge_id: str, max_bytes: int, wait_ms: int):
        nonlocal read_calls
        assert bridge_id == "bridge-1"
        assert max_bytes == 65_536
        assert wait_ms > 0
        read_calls += 1
        if read_calls == 1:
            payload = b"pty-output"
            return {
                "bridge_id": bridge_id,
                "data_b64": base64.b64encode(payload).decode("ascii"),
                "bytes": len(payload),
                "eof": False,
            }
        await release_eof.wait()
        return {
            "bridge_id": bridge_id,
            "data_b64": "",
            "bytes": 0,
            "eof": True,
        }

    async def fake_write(bridge_id: str, data_b64: str):
        assert bridge_id == "bridge-1"
        writes.append(base64.b64decode(data_b64))
        return {"bridge_id": bridge_id, "written_bytes": len(writes[-1])}

    async def fake_resize(bridge_id: str, cols: int, rows: int):
        assert bridge_id == "bridge-1"
        resizes.append((cols, rows))
        return {
            "bridge_id": bridge_id,
            "cols": cols,
            "rows": rows,
            "resized": True,
            "backend": "fake-pty",
        }

    async def fake_close(bridge_id: str):
        closed_bridges.append(bridge_id)
        return {"bridge_id": bridge_id, "closed": True}

    monkeypatch.setattr(stream_module, "_NoRedirectConnect", fake_connect)
    monkeypatch.setattr(
        stream_module, "open_terminal_bridge_execute", fake_open
    )
    monkeypatch.setattr(
        stream_module, "read_terminal_bridge_execute", fake_read
    )
    monkeypatch.setattr(
        stream_module, "write_terminal_bridge_execute", fake_write
    )
    monkeypatch.setattr(
        stream_module, "resize_terminal_bridge_execute", fake_resize
    )
    monkeypatch.setattr(
        stream_module, "close_terminal_bridge_execute", fake_close
    )

    profile = _profile()
    terminal_stream = await stream_module.connect_executor_terminal_stream(
        profile,
        stream_id="stream_abcdefghijklmnopqrstuvwxyz",
        shell_id="shell-1",
        cols=100,
        rows=30,
    )
    assert len(connect_calls) == 1
    url, kwargs = connect_calls[0]
    assert (
        url
        == "wss://control.example/executor/v1/streams/stream_abcdefghijklmnopqrstuvwxyz"
    )
    assert kwargs["additional_headers"] == {
        "Authorization": f"Bearer {profile.credential}"
    }
    assert kwargs["compression"] is None
    assert kwargs["max_queue"] == 1

    await websocket.incoming.put(b"browser-input")
    await websocket.incoming.put(
        json.dumps({"type": "resize", "cols": 120, "rows": 40})
    )

    task = asyncio.create_task(terminal_stream.run())
    for _ in range(50):
        if writes and resizes and b"pty-output" in websocket.sent:
            break
        await asyncio.sleep(0)

    assert writes == [b"browser-input"]
    assert resizes == [(120, 40)]
    assert b"pty-output" in websocket.sent
    ready = next(value for value in websocket.sent if isinstance(value, str))
    assert json.loads(ready)["type"] == "ready"

    release_eof.set()
    await asyncio.wait_for(task, timeout=0.5)

    assert websocket.closed is True
    assert closed_bridges == ["bridge-1"]
    assert any(
        isinstance(value, str) and json.loads(value).get("type") == "exit"
        for value in websocket.sent
    )


@pytest.mark.asyncio
async def test_executor_terminal_stream_validates_text_control_frames(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    websocket = _FakeWebSocket()
    writes: list[bytes] = []
    resizes: list[tuple[int, int]] = []

    async def fake_write(bridge_id: str, data_b64: str):
        assert bridge_id == "bridge-1"
        writes.append(base64.b64decode(data_b64))
        return {"bridge_id": bridge_id, "written_bytes": len(writes[-1])}

    async def fake_resize(bridge_id: str, cols: int, rows: int):
        assert bridge_id == "bridge-1"
        resizes.append((cols, rows))
        return {
            "bridge_id": bridge_id,
            "cols": cols,
            "rows": rows,
            "resized": True,
            "backend": "fake-pty",
        }

    monkeypatch.setattr(
        stream_module, "write_terminal_bridge_execute", fake_write
    )
    monkeypatch.setattr(
        stream_module, "resize_terminal_bridge_execute", fake_resize
    )

    terminal_stream = stream_module.ExecutorTerminalStream(
        profile=_profile(),
        stream_id="stream_abcdefghijklmnopqrstuvwxyz",
        shell_id="shell-1",
        bridge_id="bridge-1",
        backend="fake-pty",
        websocket=cast(Any, websocket),
    )

    await terminal_stream._handle_control(
        '{"type":"input","data":"echo ok","enter":true}'
    )
    await terminal_stream._handle_control(
        '{"type":"resize","cols":121,"rows":41}'
    )
    await terminal_stream._handle_control('{"type":"ping"}')

    assert writes == [b"echo ok\r"]
    assert resizes == [(121, 41)]
    assert any(
        isinstance(value, str) and json.loads(value).get("type") == "pong"
        for value in websocket.sent
    )

    with pytest.raises(ValueError, match="must be valid JSON"):
        await terminal_stream._handle_control("not-json")
    with pytest.raises(ValueError, match="must be an object"):
        await terminal_stream._handle_control("[]")
    with pytest.raises(ValueError, match="Unsupported terminal control frame"):
        await terminal_stream._handle_control('{"type":"unknown"}')
    with pytest.raises(ValueError, match="cols must be an integer"):
        await terminal_stream._handle_control(
            '{"type":"resize","cols":"bad","rows":41}'
        )
    with pytest.raises(ValueError, match="rows must be between"):
        await terminal_stream._handle_control(
            '{"type":"resize","cols":121,"rows":1}'
        )

    await terminal_stream._handle_control('{"type":"close"}')
    assert websocket.closed is True


@pytest.mark.asyncio
async def test_executor_terminal_stream_ready_failure_closes_bridge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    websocket = _FailingReadyWebSocket()
    closed_bridges: list[str] = []

    async def fake_connect(url: str, **kwargs: Any):
        return websocket

    async def fake_open(shell_id: str, cols: int, rows: int):
        return {
            "bridge_id": "bridge-ready-failure",
            "shell_id": shell_id,
            "cols": cols,
            "rows": rows,
            "backend": "fake-pty",
        }

    async def fake_close(bridge_id: str):
        closed_bridges.append(bridge_id)
        return {"bridge_id": bridge_id, "closed": True}

    monkeypatch.setattr(stream_module, "_NoRedirectConnect", fake_connect)
    monkeypatch.setattr(
        stream_module, "open_terminal_bridge_execute", fake_open
    )
    monkeypatch.setattr(
        stream_module, "close_terminal_bridge_execute", fake_close
    )

    terminal_stream = await stream_module.connect_executor_terminal_stream(
        _profile(),
        stream_id="stream_abcdefghijklmnopqrstuvwxyz",
        shell_id="shell-1",
        cols=100,
        rows=30,
    )
    with pytest.raises(RuntimeError, match="stream closed before ready"):
        await terminal_stream.run()

    assert websocket.closed is True
    assert closed_bridges == ["bridge-ready-failure"]


@pytest.mark.asyncio
async def test_executor_terminal_stream_bad_handshake_closes_bridge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    websocket = _BadHandshakeWebSocket()
    closed_bridges: list[str] = []

    async def fake_connect(url: str, **kwargs: Any):
        return websocket

    async def fake_open(shell_id: str, cols: int, rows: int):
        return {
            "bridge_id": "bridge-bad-handshake",
            "shell_id": shell_id,
            "cols": cols,
            "rows": rows,
            "backend": "fake-pty",
        }

    async def fake_close(bridge_id: str):
        closed_bridges.append(bridge_id)
        return {"bridge_id": bridge_id, "closed": True}

    monkeypatch.setattr(stream_module, "_NoRedirectConnect", fake_connect)
    monkeypatch.setattr(
        stream_module, "open_terminal_bridge_execute", fake_open
    )
    monkeypatch.setattr(
        stream_module, "close_terminal_bridge_execute", fake_close
    )

    with pytest.raises(
        RuntimeError, match="rejected terminal stream handshake"
    ):
        await stream_module.connect_executor_terminal_stream(
            _profile(),
            stream_id="stream_abcdefghijklmnopqrstuvwxyz",
            shell_id="shell-1",
            cols=100,
            rows=30,
        )

    assert websocket.closed is True
    assert closed_bridges == ["bridge-bad-handshake"]


@pytest.mark.asyncio
async def test_executor_terminal_stream_rejects_invalid_stream_id_before_bridge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened = False

    async def fake_open(*args: Any, **kwargs: Any):
        nonlocal opened
        opened = True
        raise AssertionError("bridge must not open")

    monkeypatch.setattr(
        stream_module, "open_terminal_bridge_execute", fake_open
    )

    with pytest.raises(ValueError, match="Invalid terminal stream_id"):
        await stream_module.connect_executor_terminal_stream(
            _profile(),
            stream_id="../bad",
            shell_id="shell-1",
            cols=100,
            rows=30,
        )
    assert opened is False


def test_executor_terminal_stream_refuses_websocket_redirects() -> None:
    connector = stream_module._NoRedirectConnect(
        "wss://control.example/executor/v1/streams/stream_abcdefghijklmnopqrstuvwxyz"
    )
    redirect = InvalidStatus(
        Response(
            302,
            "Found",
            Headers(Location="wss://redirect.example/terminal"),
        )
    )

    result = connector.process_redirect(redirect)

    assert isinstance(result, SecurityError)
    assert "redirects are disabled" in str(result)
