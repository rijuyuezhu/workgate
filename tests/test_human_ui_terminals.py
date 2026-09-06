import asyncio
import base64
import threading
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from starlette.websockets import WebSocketDisconnect

import workgate.ui.http.common as ui_common_module
import workgate.ui.http.terminals as terminal_module
from workgate.config.settings import Settings, clear_settings_cache
from workgate.control.http.app import build_http_app
from workgate.oauth.core.scopes import (
    SCOPE_REMOTE_USE,
    SCOPE_SHELL_EXECUTE,
    SCOPE_SHELL_READ,
)
from workgate.oauth.protocol.token_codec import issue_access_token
from workgate.schemas.result_models.shell import (
    KillPersistentShellOutput,
    ListPersistentShellsOutput,
    PersistentShellInfo,
    ReadPersistentShellOutput,
    ResizePersistentShellOutput,
    SendPersistentShellInputOutput,
    StartPersistentShellOutput,
)
from workgate.ui.http.live_state import (
    build_human_ui_runtime,
    configure_human_ui_runtime,
    human_ui_runtime,
)
from workgate.ui.session import (
    UI_SESSION_BINDING_HEADER,
    UI_SESSION_BINDING_PROTOCOL_PREFIX,
    ui_session_cookie_name,
)

BASE_URL = "https://workgate.example"
UI_SESSION_BINDING = "b" * 43


def test_terminal_settings_are_bounded():
    assert Settings().ui_terminal_idle_timeout_s == 3600
    assert Settings().ui_terminal_max_connections == 8
    with pytest.raises(ValidationError):
        Settings(ui_terminal_idle_timeout_s=-1)
    for value in (0, 129):
        with pytest.raises(ValidationError):
            Settings(ui_terminal_max_connections=value)


@pytest.fixture(autouse=True)
def _reset_settings_and_connections():
    clear_settings_cache()
    runtime = build_human_ui_runtime()
    previous = configure_human_ui_runtime(runtime)
    try:
        yield
    finally:
        configure_human_ui_runtime(previous)
        clear_settings_cache()


def _configure(monkeypatch, tmp_path, *, auth_mode="none", **values):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / ".state"))
    monkeypatch.setenv("WORKGATE_AUTH_MODE", auth_mode)
    monkeypatch.setenv("WORKGATE_BASE_URL", BASE_URL)
    monkeypatch.setenv("WORKGATE_AGENT_BRIDGE_ENABLED", "false")
    monkeypatch.setenv("WORKGATE_REMOTE_ENABLED", "false")
    for name, value in values.items():
        monkeypatch.setenv(f"WORKGATE_{name.upper()}", str(value).lower())
    clear_settings_cache()


def _client(monkeypatch, tmp_path, *, auth_mode="none", **values) -> TestClient:
    _configure(monkeypatch, tmp_path, auth_mode=auth_mode, **values)
    return TestClient(
        build_http_app(),
        base_url=BASE_URL,
        client=("203.0.113.10", 50000),
    )


def test_terminal_connection_limit(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path, ui_terminal_max_connections=1)
    marker = terminal_module._reserve_connection()
    assert marker is not None
    assert terminal_module._reserve_connection() is None
    terminal_module._release_connection(marker)
    replacement = terminal_module._reserve_connection()
    assert replacement is not None
    terminal_module._release_connection(replacement)


def _bearer_token(scope: str) -> str:
    return issue_access_token(
        client_id="webui-test",
        scope=scope,
        resource=f"{BASE_URL}/mcp",
    )


def _bearer_protocol(scope: str) -> str:
    encoded = (
        base64.urlsafe_b64encode(_bearer_token(scope).encode())
        .rstrip(b"=")
        .decode()
    )
    return f"bearer.{encoded}"


def test_terminal_http_surface_dispatches_bounded_actions(
    monkeypatch, tmp_path
):
    calls: list[tuple] = []

    async def fake_list():
        return ListPersistentShellsOutput(
            shells=[PersistentShellInfo(shell_id="demo")]
        )

    async def fake_start(cwd=".", name=None, command=None):
        calls.append(("start", cwd, name, command))
        return StartPersistentShellOutput(
            shell_id="created", cwd=cwd, command=command or "/bin/sh"
        )

    async def fake_send(shell_id, input_text, enter=True):
        calls.append(("send", shell_id, input_text, enter))
        return SendPersistentShellInputOutput(
            shell_id=shell_id,
            sent_bytes=len(input_text.encode()),
            enter=enter,
        )

    async def fake_resize(shell_id, cols, rows):
        calls.append(("resize", shell_id, cols, rows))
        return ResizePersistentShellOutput(
            shell_id=shell_id,
            cols=cols,
            rows=rows,
            resized=True,
            backend="tmux",
        )

    async def fake_read(shell_id, lines=200, *, preserve_ansi=False):
        calls.append(("read", shell_id, lines, preserve_ansi))
        return ReadPersistentShellOutput(shell_id=shell_id, output="hello")

    async def fake_kill(shell_id):
        calls.append(("kill", shell_id))
        return KillPersistentShellOutput(shell_id=shell_id, killed=True)

    monkeypatch.setattr(
        terminal_module, "list_persistent_shells_execute", fake_list
    )
    monkeypatch.setattr(
        terminal_module, "start_persistent_shell_execute", fake_start
    )
    monkeypatch.setattr(
        terminal_module, "send_persistent_shell_input_execute", fake_send
    )
    monkeypatch.setattr(
        terminal_module, "resize_persistent_shell_execute", fake_resize
    )
    monkeypatch.setattr(
        terminal_module, "read_persistent_shell_output_execute", fake_read
    )
    monkeypatch.setattr(
        terminal_module, "kill_persistent_shell_execute", fake_kill
    )
    client = _client(monkeypatch, tmp_path)

    listed = client.get("/api/ui/terminals")
    assert listed.status_code == 200
    assert listed.json()["data"]["shells"][0]["shell_id"] == "demo"

    started = client.post(
        "/api/ui/terminals/start",
        json={"cwd": ".", "name": "created", "command": "/bin/sh"},
    )
    assert started.status_code == 200
    assert started.json()["data"]["shell_id"] == "created"

    sent = client.post(
        "/api/ui/terminals/send",
        json={"shell_id": "demo", "input_text": "printf ok", "enter": False},
    )
    assert sent.status_code == 200

    resized = client.post(
        "/api/ui/terminals/resize",
        json={"shell_id": "demo", "cols": 132, "rows": 41},
    )
    assert resized.status_code == 200

    read = client.get(
        "/api/ui/terminals/read", params={"shell_id": "demo", "lines": 321}
    )
    assert read.status_code == 200
    assert read.json()["data"]["output"] == "hello"

    killed = client.post("/api/ui/terminals/kill", json={"shell_id": "demo"})
    assert killed.status_code == 200
    assert calls == [
        ("start", ".", "created", "/bin/sh"),
        ("send", "demo", "printf ok", False),
        ("resize", "demo", 132, 41),
        ("read", "demo", 321, True),
        ("kill", "demo"),
    ]


def test_terminal_http_actions_require_execute_scope(monkeypatch, tmp_path):
    async def fake_list():
        return ListPersistentShellsOutput(
            shells=[PersistentShellInfo(shell_id="demo")]
        )

    async def fake_start(cwd=".", name=None, command=None):
        return StartPersistentShellOutput(shell_id="created", cwd=cwd)

    monkeypatch.setattr(
        terminal_module, "list_persistent_shells_execute", fake_list
    )
    monkeypatch.setattr(
        terminal_module, "start_persistent_shell_execute", fake_start
    )
    client = _client(monkeypatch, tmp_path, auth_mode="oauth")
    read_headers = {
        "Authorization": f"Bearer {_bearer_token(SCOPE_SHELL_READ)}"
    }
    execute_headers = {
        "Authorization": (
            "Bearer "
            + _bearer_token(f"{SCOPE_SHELL_READ} {SCOPE_SHELL_EXECUTE}")
        )
    }

    assert (
        client.get("/api/ui/terminals", headers=read_headers).status_code == 200
    )
    denied = client.post(
        "/api/ui/terminals/start", json={"cwd": "."}, headers=read_headers
    )
    assert denied.status_code == 403
    assert SCOPE_SHELL_EXECUTE in denied.text
    allowed = client.post(
        "/api/ui/terminals/start", json={"cwd": "."}, headers=execute_headers
    )
    assert allowed.status_code == 200


@pytest.mark.parametrize(
    ("path", "payload", "message"),
    [
        (
            "/api/ui/terminals/read?shell_id=bad%2Fid",
            None,
            "shell_id must be",
        ),
        (
            "/api/ui/terminals/read?shell_id=demo&lines=5001",
            None,
            "lines must be between",
        ),
        (
            "/api/ui/terminals/send",
            {
                "shell_id": "demo",
                "input_text": "x"
                * (terminal_module.UI_TERMINAL_INPUT_MAX_BYTES + 1),
            },
            "input_text exceeds",
        ),
        (
            "/api/ui/terminals/resize",
            {"shell_id": "demo", "cols": 19, "rows": 30},
            "cols must be between",
        ),
    ],
)
def test_terminal_http_surface_rejects_invalid_bounds(
    monkeypatch, tmp_path, path, payload, message
):
    client = _client(monkeypatch, tmp_path)
    response = (
        client.get(path) if payload is None else client.post(path, json=payload)
    )
    assert response.status_code == 400
    assert message in response.json()["message"]


def test_terminal_websocket_requires_oauth_and_execute_scope(
    monkeypatch, tmp_path
):
    async def fake_list():
        return ListPersistentShellsOutput(
            shells=[PersistentShellInfo(shell_id="demo")]
        )

    monkeypatch.setattr(
        terminal_module, "list_persistent_shells_execute", fake_list
    )
    client = _client(monkeypatch, tmp_path, auth_mode="oauth")

    with (
        pytest.raises(WebSocketDisconnect) as missing,
        client.websocket_connect(
            "/ui/ws/terminals/demo", subprotocols=["workgate-ui-terminal"]
        ),
    ):
        pass
    assert missing.value.code == 4401

    read_only = _bearer_protocol(SCOPE_SHELL_READ)
    with (
        pytest.raises(WebSocketDisconnect) as insufficient,
        client.websocket_connect(
            "/ui/ws/terminals/demo",
            subprotocols=["workgate-ui-terminal", read_only],
        ),
    ):
        pass
    assert insufficient.value.code == 4403


@pytest.mark.parametrize(
    "websocket_base",
    (
        BASE_URL.replace("https://", "wss://", 1),
        BASE_URL.replace("https://", "ws://", 1),
    ),
    ids=("direct-tls", "tls-terminating-proxy"),
)
def test_terminal_websocket_accepts_ui_cookie_only_from_request_origin(
    monkeypatch, tmp_path, websocket_base
):
    async def fake_list():
        return ListPersistentShellsOutput(
            shells=[PersistentShellInfo(shell_id="demo")]
        )

    async def fake_read(shell_id, lines=200, *, preserve_ansi=False):
        assert shell_id == "demo"
        assert lines == 1000
        assert preserve_ansi is True
        return ReadPersistentShellOutput(shell_id=shell_id, output="prompt$ ")

    monkeypatch.setattr(
        terminal_module, "list_persistent_shells_execute", fake_list
    )
    monkeypatch.setattr(
        terminal_module, "read_persistent_shell_output_execute", fake_read
    )
    client = _client(monkeypatch, tmp_path, auth_mode="oauth")
    token = _bearer_token(f"{SCOPE_SHELL_READ} {SCOPE_SHELL_EXECUTE}")
    session = client.post(
        "/api/ui/session/token",
        headers={
            "Origin": BASE_URL,
            "Authorization": f"Bearer {token}",
            UI_SESSION_BINDING_HEADER: UI_SESSION_BINDING,
        },
    )
    assert session.status_code == 200
    session_cookie_name = ui_session_cookie_name(BASE_URL)
    session_cookie = client.cookies.get(session_cookie_name)
    assert session_cookie
    cookie_header = f"{session_cookie_name}={session_cookie}"

    with (
        pytest.raises(WebSocketDisconnect) as wrong_origin,
        client.websocket_connect(
            f"{websocket_base}/ui/ws/terminals/demo",
            headers={
                "Origin": "https://attacker.example",
                "Cookie": cookie_header,
            },
            subprotocols=[
                "workgate-ui-terminal",
                f"{UI_SESSION_BINDING_PROTOCOL_PREFIX}{UI_SESSION_BINDING}",
            ],
        ),
    ):
        pass
    assert wrong_origin.value.code == 4403

    with (
        pytest.raises(WebSocketDisconnect) as missing_binding,
        client.websocket_connect(
            f"{websocket_base}/ui/ws/terminals/demo",
            headers={"Origin": BASE_URL, "Cookie": cookie_header},
            subprotocols=["workgate-ui-terminal"],
        ),
    ):
        pass
    assert missing_binding.value.code == 4401

    with client.websocket_connect(
        f"{websocket_base}/ui/ws/terminals/demo?lines=1000",
        headers={"Origin": BASE_URL, "Cookie": cookie_header},
        subprotocols=[
            "workgate-ui-terminal",
            f"{UI_SESSION_BINDING_PROTOCOL_PREFIX}{UI_SESSION_BINDING}",
        ],
    ) as websocket:
        assert websocket.accepted_subprotocol == "workgate-ui-terminal"
        assert websocket.receive_json() == {
            "type": "snapshot",
            "machine": "local",
            "shell_id": "demo",
            "output": "prompt$ ",
        }
        websocket.send_json({"type": "close"})


def test_terminal_websocket_reports_shell_inventory_failure(
    monkeypatch, tmp_path
):
    async def fake_list():
        raise RuntimeError("tmux unavailable")

    monkeypatch.setattr(
        terminal_module, "list_persistent_shells_execute", fake_list
    )
    client = _client(monkeypatch, tmp_path)
    with (
        pytest.raises(WebSocketDisconnect) as failure,
        client.websocket_connect(
            "/ui/ws/terminals/demo", subprotocols=["workgate-ui-terminal"]
        ),
    ):
        pass
    assert failure.value.code == 1011
    assert failure.value.reason == "Unable to inspect persistent shells"


def test_terminal_websocket_streams_snapshot_and_orders_controls(
    monkeypatch, tmp_path
):
    calls: list[tuple] = []
    input_seen = threading.Event()

    async def fake_list():
        return ListPersistentShellsOutput(
            shells=[PersistentShellInfo(shell_id="demo")]
        )

    async def fake_read(shell_id, lines=200, *, preserve_ansi=False):
        assert preserve_ansi is True
        return ReadPersistentShellOutput(
            shell_id=shell_id, output="\x1b[32mprompt$ \x1b[0m"
        )

    async def fake_send(shell_id, input_text, enter=True):
        calls.append(("send", shell_id, input_text, enter))
        input_seen.set()
        return SendPersistentShellInputOutput(
            shell_id=shell_id,
            sent_bytes=len(input_text.encode()),
            enter=enter,
        )

    async def fake_resize(shell_id, cols, rows):
        calls.append(("resize", shell_id, cols, rows))
        return ResizePersistentShellOutput(
            shell_id=shell_id,
            cols=cols,
            rows=rows,
            resized=True,
            backend="tmux",
        )

    monkeypatch.setattr(
        terminal_module, "list_persistent_shells_execute", fake_list
    )
    monkeypatch.setattr(
        terminal_module, "read_persistent_shell_output_execute", fake_read
    )
    monkeypatch.setattr(
        terminal_module, "send_persistent_shell_input_execute", fake_send
    )
    monkeypatch.setattr(
        terminal_module, "resize_persistent_shell_execute", fake_resize
    )
    client = _client(monkeypatch, tmp_path, auth_mode="oauth")
    bearer = _bearer_protocol(f"{SCOPE_SHELL_READ} {SCOPE_SHELL_EXECUTE}")

    with client.websocket_connect(
        "/ui/ws/terminals/demo?lines=1000",
        subprotocols=["workgate-ui-terminal", bearer],
    ) as websocket:
        assert websocket.accepted_subprotocol == "workgate-ui-terminal"
        assert websocket.receive_json() == {
            "type": "snapshot",
            "machine": "local",
            "shell_id": "demo",
            "output": "\x1b[32mprompt$ \x1b[0m",
        }
        websocket.send_json(
            {"type": "input", "data": "printf ok", "enter": True}
        )
        websocket.send_json({"type": "resize", "cols": 120, "rows": 36})
        websocket.send_json({"type": "ping"})
        assert websocket.receive_json() == {
            "type": "pong",
            "machine": "local",
            "shell_id": "demo",
        }
        assert input_seen.wait(timeout=1)
        websocket.send_json({"type": "close"})

    assert calls == [
        ("send", "demo", "printf ok", True),
        ("resize", "demo", 120, 36),
    ]
    assert human_ui_runtime().terminal_connections.active_count() == 0


def test_terminal_bridge_read_normalization_accepts_empty_poll():
    handle = terminal_module._TerminalBridgeHandle(
        machine="local",
        shell_id="demo",
        bridge_id="bridge_capability_1234567890",
        cols=100,
        rows=30,
        backend="tmux-pty",
    )

    assert terminal_module._normalize_bridge_read(
        handle,
        {
            "bridge_id": handle.bridge_id,
            "data_b64": "",
            "bytes": 0,
            "eof": False,
        },
    ) == (b"", False)


def test_terminal_bridge_normalization_accepts_conpty_without_resize():
    handle = terminal_module._normalize_bridge_open(
        "edge",
        "demo",
        100,
        30,
        {
            "bridge_id": "bridge_capability_1234567890",
            "shell_id": "demo",
            "cols": 100,
            "rows": 30,
            "backend": "conpty",
        },
    )
    terminal_module._normalize_bridge_resize(
        handle,
        {
            "bridge_id": handle.bridge_id,
            "cols": 120,
            "rows": 40,
            "resized": False,
            "backend": "conpty",
        },
        120,
        40,
    )
    with pytest.raises(RuntimeError, match="malformed terminal bridge resize"):
        terminal_module._normalize_bridge_resize(
            handle,
            {
                "bridge_id": handle.bridge_id,
                "cols": 120,
                "rows": 40,
                "resized": False,
                "backend": "tmux-pty",
            },
            120,
            40,
        )


def test_terminal_websocket_raw_pty_streams_binary_and_closes_bridge(
    monkeypatch, tmp_path
):
    calls: list[tuple] = []
    read_count = 0

    async def fake_list():
        return ListPersistentShellsOutput(
            shells=[PersistentShellInfo(shell_id="demo")]
        )

    async def fake_open(shell_id, cols, rows):
        calls.append(("open", shell_id, cols, rows))
        return {
            "bridge_id": "bridge_capability_1234567890",
            "shell_id": shell_id,
            "cols": cols,
            "rows": rows,
            "backend": "tmux-pty",
        }

    blocked_read = asyncio.Event()

    async def fake_read(bridge_id, max_bytes=65_536, wait_ms=100):
        nonlocal read_count
        read_count += 1
        if read_count > 1:
            await blocked_read.wait()
        await asyncio.sleep(0.01)
        payload = b"\x1b[?1049hRAW\xff"
        return {
            "bridge_id": bridge_id,
            "data_b64": base64.b64encode(payload).decode(),
            "bytes": len(payload),
            "eof": False,
        }

    async def fake_write(bridge_id, data_b64):
        data = base64.b64decode(data_b64)
        calls.append(("write", bridge_id, data))
        return {"bridge_id": bridge_id, "written_bytes": len(data)}

    async def fake_resize(bridge_id, cols, rows):
        calls.append(("resize", bridge_id, cols, rows))
        return {
            "bridge_id": bridge_id,
            "cols": cols,
            "rows": rows,
            "resized": True,
            "backend": "tmux-pty",
        }

    async def fake_close(bridge_id):
        calls.append(("close", bridge_id))
        return {"bridge_id": bridge_id, "closed": True}

    monkeypatch.setattr(
        terminal_module, "list_persistent_shells_execute", fake_list
    )
    monkeypatch.setattr(
        terminal_module, "open_terminal_bridge_execute", fake_open
    )
    monkeypatch.setattr(
        terminal_module, "read_terminal_bridge_execute", fake_read
    )
    monkeypatch.setattr(
        terminal_module, "write_terminal_bridge_execute", fake_write
    )
    monkeypatch.setattr(
        terminal_module, "resize_terminal_bridge_execute", fake_resize
    )
    monkeypatch.setattr(
        terminal_module, "close_terminal_bridge_execute", fake_close
    )
    client = _client(monkeypatch, tmp_path, auth_mode="oauth")
    bearer = _bearer_protocol(f"{SCOPE_SHELL_READ} {SCOPE_SHELL_EXECUTE}")

    with client.websocket_connect(
        "/ui/ws/terminals/demo?mode=auto&cols=90&rows=28",
        subprotocols=["workgate-ui-terminal", bearer],
    ) as websocket:
        assert websocket.receive_json() == {
            "type": "ready",
            "machine": "local",
            "shell_id": "demo",
            "mode": "pty",
            "backend": "tmux-pty",
        }
        assert websocket.receive_bytes() == b"\x1b[?1049hRAW\xff"
        websocket.send_bytes(b"\xff\x00")
        websocket.send_json(
            {"type": "input", "data": "echo raw", "enter": True}
        )
        websocket.send_json({"type": "resize", "cols": 120, "rows": 36})
        websocket.send_json({"type": "ping"})
        assert websocket.receive_json() == {
            "type": "pong",
            "machine": "local",
            "shell_id": "demo",
            "mode": "pty",
        }
        websocket.send_json({"type": "close"})

    bridge_id = "bridge_capability_1234567890"
    assert calls[0] == ("open", "demo", 90, 28)
    assert ("write", bridge_id, b"\xff\x00") in calls
    assert ("write", bridge_id, b"echo raw\r") in calls
    assert ("resize", bridge_id, 120, 36) in calls
    assert calls[-1] == ("close", bridge_id)
    assert human_ui_runtime().terminal_connections.active_count() == 0


def test_terminal_websocket_auto_falls_back_to_snapshot(monkeypatch, tmp_path):
    async def fake_list():
        return ListPersistentShellsOutput(
            shells=[PersistentShellInfo(shell_id="demo")]
        )

    async def fake_open(shell_id, cols, rows):
        raise terminal_module.TerminalBridgeUnsupportedError("PTY unavailable")

    async def fake_read(shell_id, lines=200, *, preserve_ansi=False):
        await asyncio.sleep(0.01)
        return ReadPersistentShellOutput(shell_id=shell_id, output="fallback$ ")

    monkeypatch.setattr(
        terminal_module, "list_persistent_shells_execute", fake_list
    )
    monkeypatch.setattr(
        terminal_module, "open_terminal_bridge_execute", fake_open
    )
    monkeypatch.setattr(
        terminal_module, "read_persistent_shell_output_execute", fake_read
    )
    client = _client(monkeypatch, tmp_path, auth_mode="oauth")
    bearer = _bearer_protocol(f"{SCOPE_SHELL_READ} {SCOPE_SHELL_EXECUTE}")

    with client.websocket_connect(
        "/ui/ws/terminals/demo?mode=auto",
        subprotocols=["workgate-ui-terminal", bearer],
    ) as websocket:
        assert websocket.receive_json()["mode"] == "snapshot"
        assert websocket.receive_json()["output"] == "fallback$ "
        websocket.send_json({"type": "close"})


class _RemoteTerminalManager:
    def __init__(self, status: str = "online") -> None:
        self.status = status

    def list_machines(self):
        return SimpleNamespace(
            machines=[SimpleNamespace(name="edge", status=self.status)]
        )


class _RemoteTerminalCalls:
    def __init__(self, *, malformed: str = "") -> None:
        self.calls: list[tuple[str, str, dict, int]] = []
        self.malformed = malformed

    async def __call__(self, machine, tool, args, timeout_s):
        self.calls.append((machine, tool, args, timeout_s))
        if self.malformed == tool:
            data = (
                {"shells": [{"shell_id": "bad id"}]}
                if tool == "list_persistent_shells"
                else {"shell_id": "wrong"}
            )
            return {"ok": True, "data": data}
        outputs = {
            "list_persistent_shells": {
                "shells": [
                    {"shell_id": "shared", "name": "edge-shell", "cwd": "/edge"}
                ]
            },
            "start_persistent_shell": {
                "shell_id": "created",
                "name": args.get("name"),
                "cwd": args.get("cwd", "."),
                "command": args.get("command") or "/bin/sh",
            },
            "send_persistent_shell_input": {
                "shell_id": args.get("shell_id"),
                "sent_bytes": len(str(args.get("input_text") or "").encode()),
                "enter": bool(args.get("enter", True)),
            },
            "resize_persistent_shell": {
                "shell_id": args.get("shell_id"),
                "cols": args.get("cols"),
                "rows": args.get("rows"),
                "resized": True,
                "backend": "tmux",
            },
            "read_persistent_shell_output": {
                "shell_id": args.get("shell_id"),
                "output": "\x1b[36medge$ \x1b[0m",
            },
            "kill_persistent_shell": {
                "shell_id": args.get("shell_id"),
                "killed": True,
                "stderr": None,
            },
        }
        return {"ok": True, "data": outputs[tool]}


@pytest.mark.asyncio
async def test_local_and_remote_terminal_adapters_share_normalized_contract(
    monkeypatch, tmp_path
):
    async def fake_list():
        return ListPersistentShellsOutput(
            shells=[
                PersistentShellInfo(
                    shell_id="shared",
                    name="edge-shell",
                    cwd="/edge",
                )
            ]
        )

    async def fake_start(cwd=".", name=None, command=None):
        return StartPersistentShellOutput(
            shell_id="created",
            name=name,
            cwd=cwd,
            command=command or "/bin/sh",
        )

    async def fake_send(shell_id, input_text, enter=True):
        return SendPersistentShellInputOutput(
            shell_id=shell_id,
            sent_bytes=len(input_text.encode()),
            enter=enter,
        )

    async def fake_resize(shell_id, cols, rows):
        return ResizePersistentShellOutput(
            shell_id=shell_id,
            cols=cols,
            rows=rows,
            resized=True,
            backend="tmux",
        )

    async def fake_read(shell_id, lines=200, *, preserve_ansi=False):
        assert preserve_ansi is True
        return ReadPersistentShellOutput(
            shell_id=shell_id,
            output="\x1b[36medge$ \x1b[0m",
        )

    async def fake_kill(shell_id):
        return KillPersistentShellOutput(
            shell_id=shell_id, killed=True, stderr=None
        )

    _configure(monkeypatch, tmp_path, remote_enabled=True)
    monkeypatch.setattr(
        ui_common_module,
        "remote_manager",
        lambda: _RemoteTerminalManager("online"),
    )
    remote_calls = _RemoteTerminalCalls()
    monkeypatch.setattr(
        terminal_module, "call_remote_worker_tool", remote_calls
    )
    monkeypatch.setattr(
        terminal_module, "list_persistent_shells_execute", fake_list
    )
    monkeypatch.setattr(
        terminal_module, "start_persistent_shell_execute", fake_start
    )
    monkeypatch.setattr(
        terminal_module,
        "send_persistent_shell_input_execute",
        fake_send,
    )
    monkeypatch.setattr(
        terminal_module, "resize_persistent_shell_execute", fake_resize
    )
    monkeypatch.setattr(
        terminal_module,
        "read_persistent_shell_output_execute",
        fake_read,
    )
    monkeypatch.setattr(
        terminal_module, "kill_persistent_shell_execute", fake_kill
    )

    local = [
        await terminal_module._list_shells("local"),
        await terminal_module._start_shell(
            "local", cwd=".", name="created", command=None
        ),
        await terminal_module._send_shell(
            "local", "shared", "printf ok", False
        ),
        await terminal_module._resize_shell("local", "shared", 120, 36),
        await terminal_module._read_shell("local", "shared", 50),
        await terminal_module._kill_shell("local", "shared"),
    ]
    remote = [
        await terminal_module._list_shells("edge"),
        await terminal_module._start_shell(
            "edge", cwd=".", name="created", command=None
        ),
        await terminal_module._send_shell("edge", "shared", "printf ok", False),
        await terminal_module._resize_shell("edge", "shared", 120, 36),
        await terminal_module._read_shell("edge", "shared", 50),
        await terminal_module._kill_shell("edge", "shared"),
    ]

    for local_result, remote_result in zip(local, remote, strict=True):
        assert local_result["machine"] == "local"
        assert local_result["remote"] is False
        assert remote_result["machine"] == "edge"
        assert remote_result["remote"] is True
        assert {
            key: value
            for key, value in local_result.items()
            if key not in {"machine", "remote"}
        } == {
            key: value
            for key, value in remote_result.items()
            if key not in {"machine", "remote"}
        }


def _remote_terminal_client(
    monkeypatch,
    tmp_path,
    calls: _RemoteTerminalCalls,
    *,
    auth_mode: str = "none",
    status: str = "online",
):
    client = _client(
        monkeypatch,
        tmp_path,
        auth_mode=auth_mode,
        remote_enabled=True,
    )
    monkeypatch.setattr(
        ui_common_module,
        "remote_manager",
        lambda: _RemoteTerminalManager(status),
    )
    monkeypatch.setattr(terminal_module, "call_remote_worker_tool", calls)
    return client


def test_remote_terminal_http_is_machine_scoped_and_sessionless(
    monkeypatch, tmp_path
):
    calls = _RemoteTerminalCalls()
    client = _remote_terminal_client(monkeypatch, tmp_path, calls)

    listed = client.get("/api/ui/terminals", params={"machine": "edge"})
    started = client.post(
        "/api/ui/terminals/start",
        json={"machine": "edge", "cwd": ".", "name": "created"},
    )
    sent = client.post(
        "/api/ui/terminals/send",
        json={
            "machine": "edge",
            "shell_id": "shared",
            "input_text": "printf ok",
            "enter": False,
        },
    )
    resized = client.post(
        "/api/ui/terminals/resize",
        json={"machine": "edge", "shell_id": "shared", "cols": 120, "rows": 36},
    )
    read = client.get(
        "/api/ui/terminals/read",
        params={"machine": "edge", "shell_id": "shared", "lines": 50},
    )
    killed = client.post(
        "/api/ui/terminals/kill",
        json={"machine": "edge", "shell_id": "shared"},
    )

    assert [
        response.status_code
        for response in (listed, started, sent, resized, read, killed)
    ] == [200] * 6
    assert listed.json()["data"] == {
        "machine": "edge",
        "remote": True,
        "shells": [
            {
                "shell_id": "shared",
                "name": "edge-shell",
                "cwd": "/edge",
                "command": None,
            }
        ],
    }
    assert started.json()["data"]["machine"] == "edge"
    assert read.json()["data"]["output"] == "\x1b[36medge$ \x1b[0m"
    assert all(
        machine == "edge" and 1 <= timeout <= 60
        for machine, _, _, timeout in calls.calls
    )
    assert all("session_id" not in args for _, _, args, _ in calls.calls)
    assert [tool for _, tool, _, _ in calls.calls] == [
        "list_persistent_shells",
        "start_persistent_shell",
        "send_persistent_shell_input",
        "resize_persistent_shell",
        "read_persistent_shell_output",
        "kill_persistent_shell",
    ]
    assert calls.calls[4][2] == {
        "shell_id": "shared",
        "lines": 50,
        "preserve_ansi": True,
    }


def test_remote_terminal_requires_scope_and_handles_offline_and_malformed(
    monkeypatch, tmp_path
):
    calls = _RemoteTerminalCalls()
    client = _remote_terminal_client(
        monkeypatch, tmp_path, calls, auth_mode="oauth"
    )
    denied = client.get(
        "/api/ui/terminals",
        params={"machine": "edge"},
        headers={"Authorization": f"Bearer {_bearer_token(SCOPE_SHELL_READ)}"},
    )
    allowed_scope = f"{SCOPE_SHELL_READ} {SCOPE_REMOTE_USE}"
    allowed = client.get(
        "/api/ui/terminals",
        params={"machine": "edge"},
        headers={"Authorization": f"Bearer {_bearer_token(allowed_scope)}"},
    )
    assert denied.status_code == 403
    assert SCOPE_REMOTE_USE in denied.text
    assert allowed.status_code == 200

    malformed_calls = _RemoteTerminalCalls(malformed="list_persistent_shells")
    malformed_client = _remote_terminal_client(
        monkeypatch, tmp_path / "malformed", malformed_calls
    )
    malformed = malformed_client.get(
        "/api/ui/terminals", params={"machine": "edge"}
    )
    assert malformed.status_code == 502

    offline_calls = _RemoteTerminalCalls()
    offline_client = _remote_terminal_client(
        monkeypatch, tmp_path / "offline", offline_calls, status="offline"
    )
    offline = offline_client.get(
        "/api/ui/terminals", params={"machine": "edge"}
    )
    assert offline.status_code == 503
    assert offline_calls.calls == []


def test_remote_terminal_websocket_uses_selected_machine(monkeypatch, tmp_path):
    calls = _RemoteTerminalCalls()
    client = _remote_terminal_client(
        monkeypatch, tmp_path, calls, auth_mode="oauth"
    )
    scope = f"{SCOPE_SHELL_READ} {SCOPE_SHELL_EXECUTE} {SCOPE_REMOTE_USE}"
    bearer = _bearer_protocol(scope)

    with client.websocket_connect(
        "/ui/ws/terminals/shared?machine=edge&lines=50",
        subprotocols=["workgate-ui-terminal", bearer],
    ) as websocket:
        assert websocket.receive_json() == {
            "type": "snapshot",
            "machine": "edge",
            "shell_id": "shared",
            "output": "\x1b[36medge$ \x1b[0m",
        }
        websocket.send_json(
            {"type": "input", "data": "echo edge", "enter": True}
        )
        websocket.send_json({"type": "resize", "cols": 100, "rows": 30})
        websocket.send_json({"type": "ping"})
        assert websocket.receive_json() == {
            "type": "pong",
            "machine": "edge",
            "shell_id": "shared",
        }
        websocket.send_json({"type": "close"})

    tools = [tool for _, tool, _, _ in calls.calls]
    assert tools[0] == "list_persistent_shells"
    assert "read_persistent_shell_output" in tools
    assert "send_persistent_shell_input" in tools
    assert "resize_persistent_shell" in tools
    assert all("session_id" not in args for _, _, args, _ in calls.calls)
    read_args = [
        args
        for _, tool, args, _ in calls.calls
        if tool == "read_persistent_shell_output"
    ]
    assert read_args
    assert all(args.get("preserve_ansi") is True for args in read_args)


def test_remote_terminal_websocket_raw_bridge_is_sessionless(
    monkeypatch, tmp_path
):
    bridge_id = "remote_bridge_capability_1234567890"
    calls: list[tuple[str, str, dict, int]] = []
    read_count = 0

    async def remote_call(machine, tool, args, timeout_s):
        nonlocal read_count
        calls.append((machine, tool, args, timeout_s))
        if tool == "list_persistent_shells":
            data = {"shells": [{"shell_id": "shared", "cwd": "/edge"}]}
        elif tool == "open_terminal_bridge":
            data = {
                "bridge_id": bridge_id,
                "shell_id": "shared",
                "cols": args["cols"],
                "rows": args["rows"],
                "backend": "tmux-pty",
            }
        elif tool == "read_terminal_bridge":
            await asyncio.sleep(0.01)
            read_count += 1
            payload = b"REMOTE_RAW" if read_count == 1 else b""
            data = {
                "bridge_id": bridge_id,
                "data_b64": base64.b64encode(payload).decode(),
                "bytes": len(payload),
                "eof": False,
            }
        elif tool == "write_terminal_bridge":
            data = {
                "bridge_id": bridge_id,
                "written_bytes": len(base64.b64decode(args["data_b64"])),
            }
        elif tool == "resize_terminal_bridge":
            data = {
                "bridge_id": bridge_id,
                "cols": args["cols"],
                "rows": args["rows"],
                "resized": True,
                "backend": "tmux-pty",
            }
        elif tool == "close_terminal_bridge":
            data = {"bridge_id": bridge_id, "closed": True}
        else:
            raise AssertionError(tool)
        return {"ok": True, "data": data}

    client = _client(
        monkeypatch,
        tmp_path,
        auth_mode="oauth",
        remote_enabled=True,
    )
    monkeypatch.setattr(
        ui_common_module,
        "remote_manager",
        lambda: _RemoteTerminalManager("online"),
    )
    monkeypatch.setattr(terminal_module, "call_remote_worker_tool", remote_call)
    scope = f"{SCOPE_SHELL_READ} {SCOPE_SHELL_EXECUTE} {SCOPE_REMOTE_USE}"
    bearer = _bearer_protocol(scope)

    with client.websocket_connect(
        "/ui/ws/terminals/shared?machine=edge&mode=auto&cols=100&rows=30",
        subprotocols=["workgate-ui-terminal", bearer],
    ) as websocket:
        ready = websocket.receive_json()
        assert ready == {
            "type": "ready",
            "machine": "edge",
            "shell_id": "shared",
            "mode": "pty",
            "backend": "tmux-pty",
        }
        assert bridge_id not in str(ready)
        assert websocket.receive_bytes() == b"REMOTE_RAW"
        websocket.send_bytes(b"\xffremote")
        websocket.send_json({"type": "resize", "cols": 110, "rows": 32})
        websocket.send_json({"type": "close"})

    tools = [tool for _, tool, _, _ in calls]
    assert tools[0:2] == ["list_persistent_shells", "open_terminal_bridge"]
    assert "read_terminal_bridge" in tools
    assert "write_terminal_bridge" in tools
    assert "resize_terminal_bridge" in tools
    assert tools[-1] == "close_terminal_bridge"
    assert all(machine == "edge" for machine, _, _, _ in calls)
    assert all("session_id" not in args for _, _, args, _ in calls)
    write_args = [
        args for _, tool, args, _ in calls if tool == "write_terminal_bridge"
    ]
    assert base64.b64decode(write_args[0]["data_b64"]) == b"\xffremote"


def test_remote_terminal_auto_falls_back_for_older_worker(
    monkeypatch, tmp_path
):
    calls: list[tuple[str, str, dict, int]] = []

    async def remote_call(machine, tool, args, timeout_s):
        calls.append((machine, tool, args, timeout_s))
        if tool == "list_persistent_shells":
            return {
                "ok": True,
                "data": {"shells": [{"shell_id": "shared", "cwd": "/edge"}]},
            }
        if tool == "open_terminal_bridge":
            return {
                "ok": False,
                "error": "ValueError",
                "message": "Unsupported remote worker tool: open_terminal_bridge",
            }
        if tool == "read_persistent_shell_output":
            return {
                "ok": True,
                "data": {"shell_id": "shared", "output": "legacy edge$ "},
            }
        raise AssertionError(tool)

    client = _client(
        monkeypatch,
        tmp_path,
        auth_mode="oauth",
        remote_enabled=True,
    )
    monkeypatch.setattr(
        ui_common_module,
        "remote_manager",
        lambda: _RemoteTerminalManager("online"),
    )
    monkeypatch.setattr(terminal_module, "call_remote_worker_tool", remote_call)
    scope = f"{SCOPE_SHELL_READ} {SCOPE_SHELL_EXECUTE} {SCOPE_REMOTE_USE}"
    bearer = _bearer_protocol(scope)

    with client.websocket_connect(
        "/ui/ws/terminals/shared?machine=edge&mode=auto",
        subprotocols=["workgate-ui-terminal", bearer],
    ) as websocket:
        assert websocket.receive_json()["mode"] == "snapshot"
        assert websocket.receive_json()["output"] == "legacy edge$ "
        websocket.send_json({"type": "close"})

    tools = [tool for _, tool, _, _ in calls]
    assert tools[:3] == [
        "list_persistent_shells",
        "open_terminal_bridge",
        "read_persistent_shell_output",
    ]
    assert "close_terminal_bridge" not in tools


def test_remote_terminal_websocket_requires_remote_scope(monkeypatch, tmp_path):
    calls = _RemoteTerminalCalls()
    client = _remote_terminal_client(
        monkeypatch, tmp_path, calls, auth_mode="oauth"
    )
    bearer = _bearer_protocol(f"{SCOPE_SHELL_READ} {SCOPE_SHELL_EXECUTE}")

    with (
        pytest.raises(WebSocketDisconnect) as exc_info,
        client.websocket_connect(
            "/ui/ws/terminals/shared?machine=edge",
            subprotocols=["workgate-ui-terminal", bearer],
        ),
    ):
        pass

    assert exc_info.value.code == 4403
    assert calls.calls == []


@pytest.mark.asyncio
async def test_terminal_bridge_malformed_open_is_closed(monkeypatch):
    closed = []
    bridge_id = "bridge_capability_1234567890"

    async def fake_open(shell_id, cols, rows):
        return {
            "bridge_id": bridge_id,
            "shell_id": shell_id,
            "cols": cols + 1,
            "rows": rows,
            "backend": "tmux-pty",
        }

    async def fake_close(value):
        closed.append(value)
        return {"bridge_id": value, "closed": True}

    monkeypatch.setattr(
        terminal_module, "open_terminal_bridge_execute", fake_open
    )
    monkeypatch.setattr(
        terminal_module, "close_terminal_bridge_execute", fake_close
    )

    with pytest.raises(RuntimeError, match="malformed terminal bridge open"):
        await terminal_module._open_bridge("local", "demo", 80, 24)

    assert closed == [bridge_id]


def test_remote_terminal_rejects_malformed_envelope_and_oversized_output():
    with pytest.raises(RuntimeError, match="malformed terminal envelope"):
        terminal_module._remote_result_data([], machine="edge", tool="list")

    oversized = {
        "shell_id": "shared",
        "output": "x" * (terminal_module.UI_TERMINAL_OUTPUT_MAX_BYTES + 1),
    }

    with pytest.raises(RuntimeError, match="oversized terminal output"):
        terminal_module._normalize_read("edge", "shared", 50, oversized)
