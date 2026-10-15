"""WebUI native GUI observation stays session-bound, privileged and non-persistent."""

import pytest
from fastapi.testclient import TestClient
from mcp.types import CallToolResult, ImageContent

from tests.executor.test_gui_service import FakeGuiBackend
from tests.support.paired import build_paired_http_app
from workgate.config.settings import clear_settings_cache, get_settings
from workgate.errors import GuiUnavailableError
from workgate.oauth.core.scopes import SCOPE_GUI_USE, SCOPE_SHELL_READ
from workgate.oauth.protocol.token_codec import issue_access_token

BASE_URL = "https://workgate.example"


async def _setup(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKGATE_DEFAULT_WORKDIR", str(tmp_path))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / ".state"))
    monkeypatch.setenv("WORKGATE_AGENT_BRIDGE_ENABLED", "false")
    monkeypatch.setenv("WORKGATE_AUTH_MODE", "oauth")
    monkeypatch.setenv("WORKGATE_BASE_URL", BASE_URL)
    clear_settings_cache()
    (tmp_path / "project").mkdir(parents=True, exist_ok=True)
    app, harness = build_paired_http_app(get_settings())
    started = await harness.control.session_coordinator.start_session(
        workdir="project",
        executor_id=harness.executor_id,
    )
    assert isinstance(started, dict)
    client = TestClient(app, base_url=BASE_URL, client=("203.0.113.14", 50010))
    return client, harness, str(started["session_id"])


def _headers(scopes):
    token = issue_access_token(
        client_id="webui-gui-test", scope=scopes, resource=f"{BASE_URL}/mcp"
    )
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_gui_view_requires_gui_scope_and_explicit_session(
    tmp_path, monkeypatch
):
    client, harness, session_id = await _setup(tmp_path, monkeypatch)
    calls = []

    async def observed(name, args):
        calls.append((name, args))
        return {"windows": []}

    monkeypatch.setattr(
        harness.control.session_coordinator, "call_session_tool", observed
    )
    denied = client.get(
        "/api/ui/gui/windows",
        params={"session_id": session_id},
        headers=_headers(SCOPE_SHELL_READ),
    )
    assert denied.status_code == 403
    assert not calls
    allowed = _headers(SCOPE_GUI_USE)
    assert client.get("/api/ui/gui/windows", headers=allowed).status_code == 400
    assert (
        client.get(
            "/api/ui/gui/state",
            params={"session_id": session_id},
            headers=allowed,
        ).status_code
        == 400
    )
    assert not calls
    listed = client.get(
        "/api/ui/gui/windows",
        params={"session_id": session_id},
        headers=allowed,
    )
    assert listed.status_code == 200
    assert listed.json()["data"]["windows"] == []
    assert listed.headers["Cache-Control"] == "no-store"
    assert calls == [("gui_list", {"session_id": session_id})]


@pytest.mark.asyncio
async def test_gui_screenshot_reuses_native_snapshot_without_semantic_values(
    tmp_path, monkeypatch
):
    client, harness, session_id = await _setup(tmp_path, monkeypatch)
    calls = []

    async def observed(name, args):
        calls.append((name, args))
        return CallToolResult(
            content=[
                ImageContent(
                    type="image", mime_type="image/png", data="aGVsbG8="
                )
            ],
            structured_content={
                "window": {"id": "atspi:1:stable", "title": "Test"},
                "capabilities": {"coordinate_input": True},
                "state_id": "private-executor-state",
                "elements": [{"name": "Sensitive text"}],
            },
        ).model_dump(mode="json")

    monkeypatch.setattr(
        harness.control.session_coordinator, "call_session_tool", observed
    )
    response = client.get(
        "/api/ui/gui/state",
        params={"session_id": session_id, "window_id": "atspi:1:stable"},
        headers=_headers(SCOPE_GUI_USE),
    )
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json()["data"] == {
        "window": {"id": "atspi:1:stable", "title": "Test"},
        "capabilities": {"coordinate_input": True},
        "state_id": "private-executor-state",
        "state_ttl_s": None,
        "image": {"mime_type": "image/png", "data": "aGVsbG8="},
    }
    assert calls == [
        (
            "gui_state",
            {
                "session_id": session_id,
                "window_id": "atspi:1:stable",
                "screenshot": True,
                "include_elements": False,
                "max_elements": 1,
                "max_depth": 1,
            },
        )
    ]


@pytest.mark.asyncio
async def test_gui_view_ended_and_unknown_sessions_do_not_invoke_backend(
    tmp_path, monkeypatch
):
    client, harness, session_id = await _setup(tmp_path, monkeypatch)
    headers = _headers(SCOPE_GUI_USE)
    unknown = client.get(
        "/api/ui/gui/windows",
        params={"session_id": "sess_missing"},
        headers=headers,
    )
    assert unknown.status_code != 200
    await harness.control.session_coordinator.end_session(session_id)
    ended = client.get(
        "/api/ui/gui/windows",
        params={"session_id": session_id},
        headers=headers,
    )
    assert ended.status_code != 200


@pytest.mark.asyncio
async def test_gui_view_missing_capture_returns_error_without_fallback(
    tmp_path, monkeypatch
):
    client, harness, session_id = await _setup(tmp_path, monkeypatch)

    async def unavailable(name, args):
        raise GuiUnavailableError("Screenshot backend unavailable")

    monkeypatch.setattr(
        harness.control.session_coordinator, "call_session_tool", unavailable
    )
    response = client.get(
        "/api/ui/gui/state",
        params={"session_id": session_id, "window_id": "window"},
        headers=_headers(SCOPE_GUI_USE),
    )
    assert response.status_code == 502
    assert response.json()["ok"] is False
    assert response.headers["Cache-Control"] == "no-store"


@pytest.mark.asyncio
async def test_gui_view_real_paired_executor_gui_service(tmp_path, monkeypatch):
    """Exercise control routing and executor-side native GUI service, not a fake HTTP result."""
    client, harness, session_id = await _setup(tmp_path, monkeypatch)
    # WSL test runner has no live desktop; expose a fake GUI-capable executor.
    monkeypatch.setattr(
        "workgate.executor.hello.gui_capability_available", lambda: True
    )
    harness.executor.gui._backend = FakeGuiBackend()
    headers = _headers(SCOPE_GUI_USE)
    listed = client.get(
        "/api/ui/gui/windows",
        params={"session_id": session_id},
        headers=headers,
    )
    assert listed.status_code == 200, listed.text
    windows = listed.json()["data"]["windows"]
    assert [window["id"] for window in windows] == ["window-1"]
    observed = client.get(
        "/api/ui/gui/state",
        params={"session_id": session_id, "window_id": "window-1"},
        headers=headers,
    )
    assert observed.status_code == 200, observed.text
    state = observed.json()["data"]
    assert state["window"]["id"] == "window-1"
    assert state["image"]["mime_type"] == "image/png"
    assert len(state["image"]["data"]) > 16
    assert isinstance(state["state_id"], str)
    assert state["state_ttl_s"] > 0
    assert "elements" not in state
    # An unrelated executor selector must not override explicit session routing.
    second = await harness.control.session_coordinator.start_session(
        workdir="project",
        executor_id=harness.executor_id,
    )
    assert isinstance(second, dict)
    other_session = str(second["session_id"])
    other = client.get(
        "/api/ui/gui/windows",
        params={
            "session_id": other_session,
            "executor_id": "unrelated-executor",
        },
        headers=headers,
    )
    assert other.status_code == 200, other.text
    assert other.json()["data"]["session_id"] == other_session


@pytest.mark.asyncio
async def test_gui_manual_action_enforces_scope_bounds_and_redaction(
    tmp_path, monkeypatch
):
    client, harness, session_id = await _setup(tmp_path, monkeypatch)
    calls = []

    async def observed(name, args):
        calls.append((name, args))
        return {
            "state_consumed": True,
            "actions": [{"text": "PRIVATE_TEST_VALUE"}],
        }

    monkeypatch.setattr(
        harness.control.session_coordinator, "call_session_tool", observed
    )
    body = {
        "session_id": session_id,
        "window_id": "window-1",
        "state_id": "a" * 32,
        "action": {"type": "type", "text": "PRIVATE_TEST_VALUE"},
    }
    denied = client.post(
        "/api/ui/gui/action", headers=_headers(SCOPE_SHELL_READ), json=body
    )
    assert denied.status_code == 403 and calls == []
    valid = _headers(SCOPE_GUI_USE)
    assert (
        client.post(
            "/api/ui/gui/action", headers=valid, json={**body, "machine": "hp"}
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/api/ui/gui/action",
            headers=valid,
            json={**body, "state_id": "bogus"},
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/api/ui/gui/action",
            headers=valid,
            json={
                **body,
                "action": {"type": "click", "x": 1, "y": 1, "extra": "fail"},
            },
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/api/ui/gui/action", headers=valid, content=b"x" * (64 * 1024 + 1)
        ).status_code
        == 413
    )
    # Only actions actually exposed by Manual mode belong on this HTTP surface.
    for unsupported in (
        {"type": "wait", "seconds": 30},
        {"type": "set_value", "element_id": "any", "text": "x"},
        {"type": "move", "x": 1, "y": 1},
    ):
        assert (
            client.post(
                "/api/ui/gui/action",
                headers=valid,
                json={**body, "action": unsupported},
            ).status_code
            == 400
        )
    assert calls == []
    response = client.post("/api/ui/gui/action", headers=valid, json=body)
    assert response.status_code == 200, response.text
    assert response.json()["data"] == {"performed": True}
    assert "PRIVATE_TEST_VALUE" not in response.text
    assert response.headers["Cache-Control"] == "no-store"
    assert calls == [
        (
            "gui_action",
            {
                "session_id": session_id,
                "window_id": "window-1",
                "state_id": "a" * 32,
                "actions": [body["action"]],
            },
        )
    ]
    # The native 4,096-character limit must also work with multibyte text.
    long_text = "汉" * 4096
    long_response = client.post(
        "/api/ui/gui/action",
        headers=valid,
        json={**body, "action": {"type": "type", "text": long_text}},
    )
    assert long_response.status_code == 200
    assert long_response.json()["data"] == {"performed": True}
    assert long_text not in long_response.text


@pytest.mark.asyncio
async def test_gui_manual_native_rejection_does_not_reveal_typed_input(
    tmp_path, monkeypatch
):
    client, harness, session_id = await _setup(tmp_path, monkeypatch)

    async def rejected(name, args):
        raise RuntimeError("failed on PRIVATE_TEST_VALUE")

    monkeypatch.setattr(
        harness.control.session_coordinator, "call_session_tool", rejected
    )
    response = client.post(
        "/api/ui/gui/action",
        headers=_headers(SCOPE_GUI_USE),
        json={
            "session_id": session_id,
            "window_id": "window-1",
            "state_id": "b" * 32,
            "action": {"type": "type", "text": "PRIVATE_TEST_VALUE"},
        },
    )
    assert response.status_code == 502
    assert "PRIVATE_TEST_VALUE" not in response.text
    assert response.headers["Cache-Control"] == "no-store"


@pytest.mark.asyncio
async def test_gui_manual_act_uses_native_one_use_state_and_session_ownership(
    tmp_path, monkeypatch
):
    client, harness, session_id = await _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "workgate.executor.hello.gui_capability_available", lambda: True
    )
    backend = FakeGuiBackend()
    harness.executor.gui._backend = backend
    headers = _headers(SCOPE_GUI_USE)
    other = await harness.control.session_coordinator.start_session(
        workdir="project", executor_id=harness.executor_id
    )
    assert isinstance(other, dict)
    other_session = str(other["session_id"])
    frame = client.get(
        "/api/ui/gui/state",
        params={"session_id": session_id, "window_id": "window-1"},
        headers=headers,
    )
    assert frame.status_code == 200, frame.text
    token = frame.json()["data"]["state_id"]
    body = {
        "session_id": session_id,
        "window_id": "window-1",
        "state_id": token,
        "action": {"type": "click", "x": 4, "y": 5},
    }
    wrong = client.post(
        "/api/ui/gui/action",
        headers=headers,
        json={**body, "session_id": other_session},
    )
    assert wrong.status_code != 200 and backend.actions == []
    good = client.post("/api/ui/gui/action", headers=headers, json=body)
    assert good.status_code == 200, good.text
    assert backend.actions[0][1]["type"] == "click"
    assert backend.actions[0][1]["x"] == 4
    assert len(backend.actions) == 1
    duplicate = client.post("/api/ui/gui/action", headers=headers, json=body)
    assert duplicate.status_code != 200 and len(backend.actions) == 1
    await harness.control.session_coordinator.end_session(session_id)
    ended = client.post("/api/ui/gui/action", headers=headers, json=body)
    assert ended.status_code != 200 and len(backend.actions) == 1


@pytest.mark.asyncio
async def test_gui_manual_stale_geometry_does_not_dispatch_input(
    tmp_path, monkeypatch
):
    client, harness, session_id = await _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "workgate.executor.hello.gui_capability_available", lambda: True
    )
    backend = FakeGuiBackend()
    harness.executor.gui._backend = backend
    headers = _headers(SCOPE_GUI_USE)
    frame = client.get(
        "/api/ui/gui/state",
        params={"session_id": session_id, "window_id": "window-1"},
        headers=headers,
    )
    token = frame.json()["data"]["state_id"]
    backend.bounds["width"] = 10
    rejected = client.post(
        "/api/ui/gui/action",
        headers=headers,
        json={
            "session_id": session_id,
            "window_id": "window-1",
            "state_id": token,
            "action": {"type": "click", "x": 22, "y": 5},
        },
    )
    assert rejected.status_code != 200
    assert backend.actions == []
