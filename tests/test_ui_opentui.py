import asyncio
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import workgate.ui.http.opentui as opentui
from workgate.config.settings import clear_settings_cache
from workgate.control.http.app import build_http_app
from workgate.ui.http.live_state import (
    build_human_ui_runtime,
    configure_human_ui_runtime,
)


@pytest.fixture(autouse=True)
def _reset_settings() -> Generator[None]:
    clear_settings_cache()
    runtime = build_human_ui_runtime()
    previous = configure_human_ui_runtime(runtime)
    try:
        yield
    finally:
        configure_human_ui_runtime(previous)
        clear_settings_cache()


def _configure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, auth: str
) -> None:
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / ".state"))
    monkeypatch.setenv("WORKGATE_AUTH_MODE", auth)
    monkeypatch.setenv("WORKGATE_REMOTE_ENABLED", "false")
    monkeypatch.setenv("WORKGATE_AGENT_BRIDGE_ENABLED", "false")
    monkeypatch.setenv("WORKGATE_UI_TUI_COMMAND", "fake-tui")
    clear_settings_cache()


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


def test_opentui_websocket_streams_process_output_and_closes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure(monkeypatch, tmp_path, auth="none")
    process = _FakeProcess()
    monkeypatch.setattr(
        opentui,
        "spawn_opentui_process",
        lambda cols, rows, cell_aspect: process,
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
        lambda cols, rows, cell_aspect: process,
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

    opentui.spawn_opentui_process(100, 30, 2.5)

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
