import asyncio
import base64
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from starlette.websockets import WebSocketDisconnect

import workgate.ui.http.terminals as terminal_module
from tests.helpers import PairedControlHarness, build_paired_http_app
from workgate.config.settings import (
    Settings,
    clear_settings_cache,
    get_settings,
)
from workgate.oauth.core.scopes import SCOPE_SHELL_EXECUTE, SCOPE_SHELL_READ
from workgate.oauth.protocol.token_codec import issue_access_token
from workgate.protocol.terminal import TerminalBridgeUnsupportedError
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
    for name, value in values.items():
        monkeypatch.setenv(f"WORKGATE_{name.upper()}", str(value).lower())
    clear_settings_cache()


class _ExecutorTestClient(TestClient):
    def __init__(self, *args: Any, executor_id: str, **kwargs: Any) -> None:
        self.executor_id = executor_id
        super().__init__(*args, **kwargs)

    def request(self, method: str, url: Any, **kwargs: Any):
        if method.upper() in {"GET", "HEAD"}:
            params = dict(kwargs.get("params") or {})
            params.setdefault("executor_id", self.executor_id)
            kwargs["params"] = params
        else:
            body = kwargs.get("json")
            if isinstance(body, dict):
                body = dict(body)
                body.setdefault("executor_id", self.executor_id)
                kwargs["json"] = body
        return super().request(method, url, **kwargs)


class _TerminalExecutorBackend:
    """Deterministic final executor-side terminal test double."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.failures: dict[str, Exception] = {}
        self.overrides: dict[str, Any] = {}
        self.shells: list[dict[str, Any]] = [
            {
                "shell_id": "demo",
                "name": None,
                "cwd": "/workspace",
                "command": None,
            }
        ]
        self.read_output = "hello"
        self.bridge_id = "bridge_capability_1234567890"
        self.bridge_backend = "tmux-pty"
        self.bridge_reads: list[bytes] = []

    async def execute(self, op: str, args: dict[str, Any]) -> Any:
        args = dict(args)
        self.calls.append((op, args))
        failure = self.failures.get(op)
        if failure is not None:
            raise failure
        if op in self.overrides:
            return self.overrides[op]

        if op == "ui.terminals.list":
            return {"shells": self.shells}
        if op == "ui.terminals.start":
            return {
                "shell_id": "created",
                "name": args.get("name"),
                "cwd": args.get("cwd", "."),
                "command": args.get("command") or "/bin/sh",
            }
        if op == "ui.terminals.send":
            input_text = str(args.get("input_text") or "")
            return {
                "shell_id": args.get("shell_id"),
                "sent_bytes": len(input_text.encode()),
                "enter": bool(args.get("enter", True)),
            }
        if op == "ui.terminals.resize":
            return {
                "shell_id": args.get("shell_id"),
                "cols": args.get("cols"),
                "rows": args.get("rows"),
                "resized": True,
                "backend": "tmux",
            }
        if op == "ui.terminals.read":
            return {
                "shell_id": args.get("shell_id"),
                "output": self.read_output,
            }
        if op == "ui.terminals.kill":
            return {
                "shell_id": args.get("shell_id"),
                "killed": True,
                "stderr": None,
            }
        if op == "ui.terminals.bridge.open":
            return {
                "bridge_id": self.bridge_id,
                "shell_id": args.get("shell_id"),
                "cols": args.get("cols"),
                "rows": args.get("rows"),
                "backend": self.bridge_backend,
            }
        if op == "ui.terminals.bridge.read":
            if self.bridge_reads:
                payload = self.bridge_reads.pop(0)
            else:
                # A real executor long-polls for bytes. Blocking here prevents a
                # test-only busy loop and is cancelled when the WebSocket closes.
                await asyncio.sleep(60)
                payload = b""
            return {
                "bridge_id": args.get("bridge_id"),
                "data_b64": base64.b64encode(payload).decode("ascii"),
                "bytes": len(payload),
                "eof": False,
            }
        if op == "ui.terminals.bridge.write":
            payload = base64.b64decode(str(args.get("data_b64") or ""))
            return {
                "bridge_id": args.get("bridge_id"),
                "written_bytes": len(payload),
            }
        if op == "ui.terminals.bridge.resize":
            return {
                "bridge_id": args.get("bridge_id"),
                "cols": args.get("cols"),
                "rows": args.get("rows"),
                "resized": True,
                "backend": self.bridge_backend,
            }
        if op == "ui.terminals.bridge.close":
            return {"bridge_id": args.get("bridge_id"), "closed": True}
        raise AssertionError(f"unexpected terminal op: {op}")


def _client(
    monkeypatch, tmp_path, *, auth_mode="none", **values
) -> tuple[_ExecutorTestClient, _TerminalExecutorBackend, PairedControlHarness]:
    _configure(monkeypatch, tmp_path, auth_mode=auth_mode, **values)
    app, harness = build_paired_http_app(get_settings())
    backend = _TerminalExecutorBackend()
    monkeypatch.setattr(
        harness.executor.ui_terminals, "execute", backend.execute
    )
    client = _ExecutorTestClient(
        app,
        executor_id=harness.executor_id,
        base_url=BASE_URL,
        client=("203.0.113.10", 50000),
    )
    return client, backend, harness


def _ws_path(client: _ExecutorTestClient, path: str) -> str:
    separator = "&" if "?" in path else "?"
    return f"{path}{separator}executor_id={client.executor_id}"


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


def test_terminal_connection_limit(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path, ui_terminal_max_connections=1)
    marker = terminal_module._reserve_connection()
    assert marker is not None
    assert terminal_module._reserve_connection() is None
    terminal_module._release_connection(marker)
    replacement = terminal_module._reserve_connection()
    assert replacement is not None
    terminal_module._release_connection(replacement)


def test_terminal_http_surface_dispatches_final_executor_ops(
    monkeypatch, tmp_path
):
    client, backend, _ = _client(monkeypatch, tmp_path)

    listed = client.get("/api/ui/terminals")
    assert listed.status_code == 200
    assert listed.json()["data"] == {
        "executor_id": client.executor_id,
        "shells": [
            {
                "shell_id": "demo",
                "name": None,
                "cwd": "/workspace",
                "command": None,
            }
        ],
    }

    started = client.post(
        "/api/ui/terminals/start",
        json={"cwd": ".", "name": "created", "command": "/bin/sh"},
    )
    sent = client.post(
        "/api/ui/terminals/send",
        json={"shell_id": "demo", "input_text": "printf ok", "enter": False},
    )
    resized = client.post(
        "/api/ui/terminals/resize",
        json={"shell_id": "demo", "cols": 132, "rows": 41},
    )
    read = client.get(
        "/api/ui/terminals/read", params={"shell_id": "demo", "lines": 321}
    )
    killed = client.post("/api/ui/terminals/kill", json={"shell_id": "demo"})

    assert [
        response.status_code
        for response in (started, sent, resized, read, killed)
    ] == [200] * 5
    assert started.json()["data"]["executor_id"] == client.executor_id
    assert started.json()["data"]["shell_id"] == "created"
    assert read.json()["data"] == {
        "executor_id": client.executor_id,
        "shell_id": "demo",
        "output": "hello",
        "lines": 321,
    }
    assert backend.calls == [
        ("ui.terminals.list", {}),
        (
            "ui.terminals.start",
            {"cwd": ".", "name": "created", "command": "/bin/sh"},
        ),
        (
            "ui.terminals.send",
            {"shell_id": "demo", "input_text": "printf ok", "enter": False},
        ),
        (
            "ui.terminals.resize",
            {"shell_id": "demo", "cols": 132, "rows": 41},
        ),
        ("ui.terminals.read", {"shell_id": "demo", "lines": 321}),
        ("ui.terminals.kill", {"shell_id": "demo"}),
    ]
    assert all("session_id" not in args for _, args in backend.calls)


def test_terminal_http_requires_explicit_eligible_executor(
    monkeypatch, tmp_path
):
    client, backend, _ = _client(monkeypatch, tmp_path)
    raw_client = TestClient(
        client.app,
        base_url=BASE_URL,
        client=("203.0.113.12", 50002),
    )

    missing = raw_client.get("/api/ui/terminals")
    unknown = raw_client.get(
        "/api/ui/terminals", params={"executor_id": "missing-executor"}
    )

    assert missing.status_code == 400
    assert "executor_id is required" in missing.json()["message"]
    assert unknown.status_code == 502
    assert "not currently eligible" in unknown.json()["message"]
    assert backend.calls == []


def test_terminal_http_actions_require_execute_scope(monkeypatch, tmp_path):
    client, backend, _ = _client(monkeypatch, tmp_path, auth_mode="oauth")
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
    allowed = client.post(
        "/api/ui/terminals/start", json={"cwd": "."}, headers=execute_headers
    )

    assert denied.status_code == 403
    assert SCOPE_SHELL_EXECUTE in denied.text
    assert allowed.status_code == 200
    assert [op for op, _ in backend.calls] == [
        "ui.terminals.list",
        "ui.terminals.start",
    ]


@pytest.mark.parametrize(
    ("method", "path", "kwargs", "message"),
    [
        (
            "get",
            "/api/ui/terminals/read",
            {"params": {"shell_id": "bad/id"}},
            "shell_id must be",
        ),
        (
            "get",
            "/api/ui/terminals/read",
            {"params": {"shell_id": "demo", "lines": 5001}},
            "lines must be between",
        ),
        (
            "post",
            "/api/ui/terminals/send",
            {
                "json": {
                    "shell_id": "demo",
                    "input_text": "x"
                    * (terminal_module.UI_TERMINAL_INPUT_MAX_BYTES + 1),
                }
            },
            "input_text exceeds",
        ),
        (
            "post",
            "/api/ui/terminals/resize",
            {"json": {"shell_id": "demo", "cols": 19, "rows": 30}},
            "cols must be between",
        ),
    ],
)
def test_terminal_http_surface_rejects_invalid_bounds(
    monkeypatch, tmp_path, method, path, kwargs, message
):
    client, backend, _ = _client(monkeypatch, tmp_path)
    response = client.request(method, path, **kwargs)
    assert response.status_code == 400
    assert message in response.json()["message"]
    assert backend.calls == []


def test_terminal_websocket_requires_oauth_and_execute_scope(
    monkeypatch, tmp_path
):
    client, backend, _ = _client(monkeypatch, tmp_path, auth_mode="oauth")
    path = _ws_path(client, "/ui/ws/terminals/demo")

    with (
        pytest.raises(WebSocketDisconnect) as missing,
        client.websocket_connect(path, subprotocols=["workgate-ui-terminal"]),
    ):
        pass
    assert missing.value.code == 4401

    read_only = _bearer_protocol(SCOPE_SHELL_READ)
    with (
        pytest.raises(WebSocketDisconnect) as insufficient,
        client.websocket_connect(
            path,
            subprotocols=["workgate-ui-terminal", read_only],
        ),
    ):
        pass
    assert insufficient.value.code == 4403
    assert backend.calls == []


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
    client, backend, _ = _client(monkeypatch, tmp_path, auth_mode="oauth")
    backend.read_output = "prompt$ "
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
    path = _ws_path(
        client,
        f"{websocket_base}/ui/ws/terminals/demo?lines=1000",
    )

    with (
        pytest.raises(WebSocketDisconnect) as wrong_origin,
        client.websocket_connect(
            path,
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
            path,
            headers={"Origin": BASE_URL, "Cookie": cookie_header},
            subprotocols=["workgate-ui-terminal"],
        ),
    ):
        pass
    assert missing_binding.value.code == 4401

    with client.websocket_connect(
        path,
        headers={"Origin": BASE_URL, "Cookie": cookie_header},
        subprotocols=[
            "workgate-ui-terminal",
            f"{UI_SESSION_BINDING_PROTOCOL_PREFIX}{UI_SESSION_BINDING}",
        ],
    ) as websocket:
        assert websocket.accepted_subprotocol == "workgate-ui-terminal"
        assert websocket.receive_json() == {
            "type": "snapshot",
            "executor_id": client.executor_id,
            "shell_id": "demo",
            "output": "prompt$ ",
        }
        websocket.send_json({"type": "close"})


def test_terminal_websocket_reports_shell_inventory_failure(
    monkeypatch, tmp_path
):
    client, backend, _ = _client(monkeypatch, tmp_path)
    backend.failures["ui.terminals.list"] = RuntimeError("tmux unavailable")

    with (
        pytest.raises(WebSocketDisconnect) as failure,
        client.websocket_connect(
            _ws_path(client, "/ui/ws/terminals/demo"),
            subprotocols=["workgate-ui-terminal"],
        ),
    ):
        pass
    assert failure.value.code == 1011
    assert failure.value.reason == "Unable to inspect persistent shells"


def test_terminal_websocket_rejects_shell_missing_from_executor_inventory(
    monkeypatch, tmp_path
):
    client, backend, _ = _client(monkeypatch, tmp_path)
    backend.shells = [{"shell_id": "other", "cwd": "/workspace"}]

    with (
        pytest.raises(WebSocketDisconnect) as failure,
        client.websocket_connect(
            _ws_path(client, "/ui/ws/terminals/demo"),
            subprotocols=["workgate-ui-terminal"],
        ),
    ):
        pass
    assert failure.value.code == 4404
    assert failure.value.reason == "Persistent shell not found"


def test_terminal_websocket_streams_snapshot_and_orders_controls(
    monkeypatch, tmp_path
):
    client, backend, _ = _client(monkeypatch, tmp_path, auth_mode="oauth")
    backend.read_output = "\x1b[32mprompt$ \x1b[0m"
    bearer = _bearer_protocol(f"{SCOPE_SHELL_READ} {SCOPE_SHELL_EXECUTE}")

    with client.websocket_connect(
        _ws_path(client, "/ui/ws/terminals/demo?lines=1000"),
        subprotocols=["workgate-ui-terminal", bearer],
    ) as websocket:
        assert websocket.accepted_subprotocol == "workgate-ui-terminal"
        assert websocket.receive_json() == {
            "type": "snapshot",
            "executor_id": client.executor_id,
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
            "executor_id": client.executor_id,
            "shell_id": "demo",
        }
        websocket.send_json({"type": "close"})

    send_call = (
        "ui.terminals.send",
        {"shell_id": "demo", "input_text": "printf ok", "enter": True},
    )
    resize_call = (
        "ui.terminals.resize",
        {"shell_id": "demo", "cols": 120, "rows": 36},
    )
    assert send_call in backend.calls
    assert resize_call in backend.calls
    assert backend.calls.index(send_call) < backend.calls.index(resize_call)
    assert human_ui_runtime().terminal_connections.active_count() == 0


def test_terminal_bridge_read_normalization_accepts_empty_poll():
    handle = terminal_module._TerminalBridgeHandle(
        executor_id="executor-a",
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
        "executor-a",
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
    client, backend, _ = _client(monkeypatch, tmp_path, auth_mode="oauth")
    payload = b"\x1b[?1049hRAW\xff"
    backend.bridge_reads.append(payload)
    bearer = _bearer_protocol(f"{SCOPE_SHELL_READ} {SCOPE_SHELL_EXECUTE}")

    with client.websocket_connect(
        _ws_path(client, "/ui/ws/terminals/demo?mode=auto&cols=90&rows=28"),
        subprotocols=["workgate-ui-terminal", bearer],
    ) as websocket:
        ready = websocket.receive_json()
        assert ready == {
            "type": "ready",
            "executor_id": client.executor_id,
            "shell_id": "demo",
            "mode": "pty",
            "backend": "tmux-pty",
        }
        assert backend.bridge_id not in str(ready)
        assert websocket.receive_bytes() == payload
        websocket.send_bytes(b"\xff\x00")
        websocket.send_json(
            {"type": "input", "data": "echo raw", "enter": True}
        )
        websocket.send_json({"type": "resize", "cols": 120, "rows": 36})
        websocket.send_json({"type": "ping"})
        assert websocket.receive_json() == {
            "type": "pong",
            "executor_id": client.executor_id,
            "shell_id": "demo",
            "mode": "pty",
        }
        websocket.send_json({"type": "close"})

    write_payloads = [
        base64.b64decode(str(args["data_b64"]))
        for op, args in backend.calls
        if op == "ui.terminals.bridge.write"
    ]
    assert b"\xff\x00" in write_payloads
    assert b"echo raw\r" in write_payloads
    assert (
        "ui.terminals.bridge.resize",
        {"bridge_id": backend.bridge_id, "cols": 120, "rows": 36},
    ) in backend.calls
    assert backend.calls[-1] == (
        "ui.terminals.bridge.close",
        {"bridge_id": backend.bridge_id},
    )
    assert all("session_id" not in args for _, args in backend.calls)
    assert human_ui_runtime().terminal_connections.active_count() == 0


def test_terminal_websocket_auto_falls_back_to_snapshot(monkeypatch, tmp_path):
    client, backend, _ = _client(monkeypatch, tmp_path, auth_mode="oauth")
    backend.failures["ui.terminals.bridge.open"] = (
        TerminalBridgeUnsupportedError("PTY unavailable")
    )
    backend.read_output = "fallback$ "
    bearer = _bearer_protocol(f"{SCOPE_SHELL_READ} {SCOPE_SHELL_EXECUTE}")

    with client.websocket_connect(
        _ws_path(client, "/ui/ws/terminals/demo?mode=auto"),
        subprotocols=["workgate-ui-terminal", bearer],
    ) as websocket:
        assert websocket.receive_json() == {
            "type": "ready",
            "executor_id": client.executor_id,
            "shell_id": "demo",
            "mode": "snapshot",
            "backend": "tmux-snapshot",
        }
        assert websocket.receive_json() == {
            "type": "snapshot",
            "executor_id": client.executor_id,
            "shell_id": "demo",
            "output": "fallback$ ",
        }
        websocket.send_json({"type": "close"})

    assert "ui.terminals.bridge.close" not in [op for op, _ in backend.calls]


def test_terminal_http_rejects_malformed_executor_inventory(
    monkeypatch, tmp_path
):
    client, backend, _ = _client(monkeypatch, tmp_path)
    backend.overrides["ui.terminals.list"] = {
        "shells": [{"shell_id": "bad id"}]
    }

    response = client.get("/api/ui/terminals")

    assert response.status_code == 502
    assert "malformed terminal inventory" in response.json()["message"]


@pytest.mark.asyncio
async def test_terminal_bridge_malformed_open_is_closed(monkeypatch, tmp_path):
    client, backend, harness = _client(monkeypatch, tmp_path)
    backend.overrides["ui.terminals.bridge.open"] = {
        "bridge_id": backend.bridge_id,
        "shell_id": "demo",
        "cols": 81,
        "rows": 24,
        "backend": "tmux-pty",
    }

    with pytest.raises(RuntimeError, match="malformed terminal bridge open"):
        await terminal_module._open_bridge(
            harness.control,
            harness.executor_id,
            "demo",
            80,
            24,
        )

    assert backend.calls == [
        (
            "ui.terminals.bridge.open",
            {"shell_id": "demo", "cols": 80, "rows": 24},
        ),
        (
            "ui.terminals.bridge.close",
            {"bridge_id": backend.bridge_id},
        ),
    ]
    client.close()


def test_terminal_read_normalization_rejects_oversized_executor_output():
    oversized = {
        "shell_id": "demo",
        "output": "x" * (terminal_module.UI_TERMINAL_OUTPUT_MAX_BYTES + 1),
    }

    with pytest.raises(RuntimeError, match="oversized terminal output"):
        terminal_module._normalize_read("executor-a", "demo", 50, oversized)
