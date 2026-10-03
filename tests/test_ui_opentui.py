import asyncio
import base64
from collections.abc import Generator
from pathlib import Path
from typing import Any

import jwt
import pytest
from fastapi.testclient import TestClient
from starlette.requests import HTTPConnection
from starlette.websockets import WebSocketDisconnect

import workgate.ui.http.opentui as opentui
import workgate.ui.http.session as ui_session_http
from workgate.config.control import resolve_control_config
from workgate.config.settings import Settings, clear_settings_cache
from workgate.control.http.app import build_http_app
from workgate.oauth.core.scopes import SCOPE_SHELL_EXECUTE, SCOPE_SHELL_READ
from workgate.oauth.protocol.token_codec import issue_access_token
from workgate.ui.session import (
    UI_SESSION_BINDING_HEADER,
    UI_SESSION_BINDING_PROTOCOL_PREFIX,
    ui_session_cookie_name,
)

BASE_URL = "https://workgate.example"
UI_SESSION_BINDING = "b" * 43


@pytest.fixture(autouse=True)
def _reset_settings() -> Generator[None]:
    clear_settings_cache()
    try:
        yield
    finally:
        clear_settings_cache()


def _configure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, auth: str
) -> None:
    monkeypatch.setenv("WORKGATE_DEFAULT_WORKDIR", str(tmp_path))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / ".state"))
    monkeypatch.setenv("WORKGATE_AUTH_MODE", auth)
    monkeypatch.setenv("WORKGATE_BASE_URL", BASE_URL)
    monkeypatch.setenv("WORKGATE_AGENT_BRIDGE_ENABLED", "false")
    monkeypatch.setenv("WORKGATE_UI_TUI_COMMAND", "fake-tui")
    clear_settings_cache()


def _bearer_protocol(scope: str) -> str:
    token = issue_access_token(
        client_id="opentui-test",
        scope=scope,
        resource=f"{BASE_URL}/mcp",
    )
    encoded = base64.urlsafe_b64encode(token.encode()).rstrip(b"=").decode()
    return f"bearer.{encoded}"


class _FakeProcess:
    def __init__(self, *, return_code: int = 0) -> None:
        self.reads = [b"OpenTUI ready\r\n", b""]
        self.return_code = return_code
        self.writes: list[bytes] = []
        self.resizes: list[tuple[int, int]] = []
        self.closed = False

    def resize(self, cols: int, rows: int) -> None:
        self.resizes.append((cols, rows))

    async def read(self) -> bytes:
        await asyncio.sleep(0)
        return self.reads.pop(0)

    async def write(self, data: bytes) -> None:
        self.writes.append(data)

    async def exit_code(self) -> int | None:
        return self.return_code

    async def close(self) -> None:
        self.closed = True


def test_opentui_websocket_requires_oauth_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure(monkeypatch, tmp_path, auth="oauth")
    client = TestClient(build_http_app(), client=("203.0.113.10", 50000))

    with (
        pytest.raises(WebSocketDisconnect) as exc_info,
        client.websocket_connect(
            "/ui/ws/opentui", subprotocols=["workgate-ui-terminal"]
        ),
    ):
        pass

    assert exc_info.value.code == 4401


def test_opentui_websocket_rejects_invalid_bearer_and_missing_execute_scope(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure(monkeypatch, tmp_path, auth="oauth")
    client = TestClient(
        build_http_app(),
        base_url=BASE_URL,
        client=("203.0.113.10", 50000),
    )

    with (
        pytest.raises(WebSocketDisconnect) as invalid,
        client.websocket_connect(
            "/ui/ws/opentui",
            subprotocols=[
                opentui.UI_OPENTUI_SUBPROTOCOL,
                "bearer.bm90LWEtand0",
            ],
        ),
    ):
        pass
    assert invalid.value.code == 4401

    with (
        pytest.raises(WebSocketDisconnect) as insufficient,
        client.websocket_connect(
            "/ui/ws/opentui",
            subprotocols=[
                opentui.UI_OPENTUI_SUBPROTOCOL,
                _bearer_protocol(SCOPE_SHELL_READ),
            ],
        ),
    ):
        pass
    assert insufficient.value.code == 4403
    assert insufficient.value.reason == (
        f"Missing required OAuth scope: {SCOPE_SHELL_EXECUTE}"
    )


def test_opentui_websocket_cookie_auth_requires_origin_and_binding(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure(monkeypatch, tmp_path, auth="oauth")
    process = _FakeProcess()
    monkeypatch.setattr(
        opentui,
        "spawn_opentui_process",
        lambda cols, rows, cell_aspect, **_kwargs: process,
    )
    client = TestClient(
        build_http_app(),
        base_url=BASE_URL,
        client=("203.0.113.10", 50000),
    )
    token = issue_access_token(
        client_id="opentui-cookie-test",
        scope=f"{SCOPE_SHELL_READ} {SCOPE_SHELL_EXECUTE}",
        resource=f"{BASE_URL}/mcp",
    )
    session = client.post(
        "/api/ui/session/token",
        headers={
            "Origin": BASE_URL,
            "Authorization": f"Bearer {token}",
            UI_SESSION_BINDING_HEADER: UI_SESSION_BINDING,
        },
    )
    assert session.status_code == 200
    cookie_name = ui_session_cookie_name(BASE_URL)
    session_cookie = client.cookies.get(cookie_name)
    assert session_cookie
    cookie_header = f"{cookie_name}={session_cookie}"
    websocket_url = BASE_URL.replace("https://", "wss://", 1) + "/ui/ws/opentui"
    protocols = [
        opentui.UI_OPENTUI_SUBPROTOCOL,
        f"{UI_SESSION_BINDING_PROTOCOL_PREFIX}{UI_SESSION_BINDING}",
    ]

    with (
        pytest.raises(WebSocketDisconnect) as wrong_origin,
        client.websocket_connect(
            websocket_url,
            headers={
                "Origin": "https://attacker.example",
                "Cookie": cookie_header,
            },
            subprotocols=protocols,
        ),
    ):
        pass
    assert wrong_origin.value.code == 4403

    with (
        pytest.raises(WebSocketDisconnect) as missing_binding,
        client.websocket_connect(
            websocket_url,
            headers={"Origin": BASE_URL, "Cookie": cookie_header},
            subprotocols=[opentui.UI_OPENTUI_SUBPROTOCOL],
        ),
    ):
        pass
    assert missing_binding.value.code == 4401

    with client.websocket_connect(
        websocket_url,
        headers={"Origin": BASE_URL, "Cookie": cookie_header},
        subprotocols=protocols,
    ) as websocket:
        assert websocket.accepted_subprotocol == opentui.UI_OPENTUI_SUBPROTOCOL
        assert websocket.receive_bytes() == b"OpenTUI ready\r\n"
        with pytest.raises(WebSocketDisconnect) as closed:
            websocket.receive_bytes()
    assert closed.value.code == 1000
    assert process.closed is True


def test_opentui_websocket_streams_process_output_and_closes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure(monkeypatch, tmp_path, auth="none")
    process = _FakeProcess()
    monkeypatch.setattr(
        opentui,
        "spawn_opentui_process",
        lambda cols, rows, cell_aspect, **_kwargs: process,
    )
    client = TestClient(build_http_app())

    with client.websocket_connect(
        "/ui/ws/opentui?cols=90&rows=28&cell_aspect=2",
        subprotocols=["workgate-ui-terminal"],
    ) as websocket:
        assert websocket.receive_bytes() == b"OpenTUI ready\r\n"
        with pytest.raises(WebSocketDisconnect) as exc_info:
            websocket.receive_bytes()

    assert exc_info.value.code == 1000

    assert process.closed is True


def test_opentui_websocket_reports_abnormal_process_exit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure(monkeypatch, tmp_path, auth="none")
    process = _FakeProcess(return_code=17)
    monkeypatch.setattr(
        opentui,
        "spawn_opentui_process",
        lambda cols, rows, cell_aspect, **_kwargs: process,
    )
    client = TestClient(build_http_app())

    with client.websocket_connect(
        "/ui/ws/opentui?cols=90&rows=28&cell_aspect=2",
        subprotocols=["workgate-ui-terminal"],
    ) as websocket:
        assert websocket.receive_bytes() == b"OpenTUI ready\r\n"
        with pytest.raises(WebSocketDisconnect) as exc_info:
            websocket.receive_bytes()

    assert exc_info.value.code == 1011
    assert exc_info.value.reason == "OpenTUI process exited with code 17"
    assert process.closed is True


def test_spawn_opentui_process_keeps_local_token_in_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure(monkeypatch, tmp_path, auth="none")
    captured: dict[str, Any] = {}

    class FakeUnix:
        def __init__(
            self,
            command: list[str],
            env: dict[str, str],
            cols: int,
            rows: int,
        ) -> None:
            captured.update(
                command=command,
                env=env,
                cols=cols,
                rows=rows,
            )

    monkeypatch.setattr(opentui, "UnixOpenTuiProcess", FakeUnix)
    monkeypatch.setattr(opentui, "WindowsOpenTuiProcess", FakeUnix)
    monkeypatch.setattr(
        opentui, "resolve_tui_command", lambda _settings: ["fake-tui"]
    )
    monkeypatch.setattr(
        opentui, "get_or_create_ui_local_token", lambda: "private-token"
    )

    opentui.spawn_opentui_process(
        100, 30, 2.5, settings=resolve_control_config(Settings())
    )

    assert captured["command"] == ["fake-tui"]
    assert "private-token" not in captured["command"]
    assert captured["env"][opentui.UI_LOCAL_TOKEN_ENV] == "private-token"
    assert captured["env"]["WORKGATE_UI_API_BASE"].endswith(":8765/api/ui")
    assert captured["env"]["WORKGATE_UI_MODE"] == "web"
    assert captured["env"]["TERM"] == "xterm-256color"
    assert captured["env"]["COLORTERM"] == "truecolor"
    assert captured["env"]["TERM_PROGRAM"] == "vscode"
    assert captured["env"]["TERM_PROGRAM_VERSION"] == (
        f"workgate/{opentui.__version__}"
    )


def test_ui_session_helpers_reject_invalid_websocket_target() -> None:
    connection = HTTPConnection(
        {
            "type": "websocket",
            "scheme": "ftp",
            "path": "/ui/ws/opentui",
            "headers": [
                (b"host", b"workgate.example"),
                (b"origin", b"https://workgate.example"),
            ],
            "query_string": b"",
        }
    )

    assert ui_session_http.has_valid_ui_origin(connection) is False
    with pytest.raises(
        jwt.InvalidTokenError, match="Invalid Human UI request origin"
    ):
        ui_session_http.ui_session_claims(connection)
