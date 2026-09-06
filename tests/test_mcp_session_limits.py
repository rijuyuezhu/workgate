import asyncio
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from starlette.types import Message, Receive, Scope, Send

from workgate.config.settings import clear_settings_cache
from workgate.control.mcp.app import build_mcp, build_mcp_http_app
from workgate.control.mcp.session_limits import (
    McpSessionLimitMiddleware,
)


def _initialize_payload() -> dict[str, object]:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "session-limit-test", "version": "1"},
        },
    }


def _mcp_headers(**extra: str) -> dict[str, str]:
    return {
        "accept": "application/json, text/event-stream",
        "content-type": "application/json",
        **extra,
    }


@pytest.mark.asyncio
async def test_session_limit_prunes_terminated_bookkeeping_and_rejects_new_session():
    calls: list[Scope] = []
    messages: list[Message] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        calls.append(scope)

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Message) -> None:
        messages.append(message)

    manager = SimpleNamespace(
        stateless=False,
        _server_instances={
            "dead": SimpleNamespace(is_terminated=True),
            "live": SimpleNamespace(is_terminated=False),
        },
        _session_owners={"dead": "old", "live": "current"},
    )
    limiter = McpSessionLimitMiddleware(
        app,
        session_manager=manager,
        max_sessions=1,
    )

    assert limiter._active_session_count() == 1
    assert "dead" not in manager._server_instances
    assert "dead" not in manager._session_owners

    await limiter(
        {"type": "http", "path": "/mcp", "method": "POST", "headers": []},
        receive,
        send,
    )
    assert calls == []
    assert messages[0]["type"] == "http.response.start"
    assert messages[0]["status"] == 429

    await limiter(
        {
            "type": "http",
            "path": "/mcp",
            "method": "POST",
            "headers": [(b"mcp-session-id", b"live")],
        },
        receive,
        send,
    )
    assert len(calls) == 1


def test_stateful_mcp_sessions_have_idle_timeout_and_capacity_limit(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / ".state"))
    monkeypatch.setenv("WORKGATE_AUTH_MODE", "none")
    monkeypatch.setenv("WORKGATE_REMOTE_ENABLED", "false")
    monkeypatch.setenv("WORKGATE_AGENT_BRIDGE_ENABLED", "false")
    monkeypatch.setenv("WORKGATE_MCP_SESSION_IDLE_TIMEOUT_S", "7")
    monkeypatch.setenv("WORKGATE_MCP_MAX_SESSIONS", "2")
    clear_settings_cache()

    mcp = build_mcp()
    app = build_mcp_http_app(mcp)
    manager = mcp._session_manager
    assert manager is not None
    assert manager.session_idle_timeout == 7

    with TestClient(app, base_url="http://127.0.0.1") as client:
        first = client.post(
            "/mcp", json=_initialize_payload(), headers=_mcp_headers()
        )
        second = client.post(
            "/mcp", json=_initialize_payload(), headers=_mcp_headers()
        )
        rejected = client.post(
            "/mcp", json=_initialize_payload(), headers=_mcp_headers()
        )

        assert first.status_code == 200
        assert second.status_code == 200
        assert rejected.status_code == 429
        assert rejected.json()["error"] == "mcp_session_limit"

        first_session = first.headers["mcp-session-id"]
        ping = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 2, "method": "ping"},
            headers=_mcp_headers(
                **{
                    "mcp-session-id": first_session,
                    "mcp-protocol-version": "2025-06-18",
                }
            ),
        )
        assert ping.status_code == 200

        deleted = client.delete(
            "/mcp",
            headers={
                "accept": "application/json",
                "mcp-session-id": first_session,
                "mcp-protocol-version": "2025-06-18",
            },
        )
        assert deleted.status_code == 200

        replacement = client.post(
            "/mcp", json=_initialize_payload(), headers=_mcp_headers()
        )
        assert replacement.status_code == 200


def test_idle_session_expiry_releases_capacity_without_delete(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / ".state"))
    monkeypatch.setenv("WORKGATE_AUTH_MODE", "none")
    monkeypatch.setenv("WORKGATE_REMOTE_ENABLED", "false")
    monkeypatch.setenv("WORKGATE_AGENT_BRIDGE_ENABLED", "false")
    monkeypatch.setenv("WORKGATE_MCP_SESSION_IDLE_TIMEOUT_S", "1")
    monkeypatch.setenv("WORKGATE_MCP_MAX_SESSIONS", "1")
    clear_settings_cache()

    mcp = build_mcp()
    app = build_mcp_http_app(mcp)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        first = client.post(
            "/mcp", json=_initialize_payload(), headers=_mcp_headers()
        )
        blocked = client.post(
            "/mcp", json=_initialize_payload(), headers=_mcp_headers()
        )
        time.sleep(1.25)
        replacement = client.post(
            "/mcp", json=_initialize_payload(), headers=_mcp_headers()
        )

    assert first.status_code == 200
    assert blocked.status_code == 429
    assert replacement.status_code == 200


def test_session_limit_fails_loudly_for_incompatible_sdk_manager():
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        return None

    manager = SimpleNamespace(stateless=False)
    with pytest.raises(RuntimeError, match="_server_instances"):
        McpSessionLimitMiddleware(
            app,
            session_manager=manager,
            max_sessions=1,
        )


def test_session_limit_fails_loudly_for_incompatible_owner_mapping():
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        return None

    manager = SimpleNamespace(
        stateless=False,
        _server_instances={},
        _session_owners=object(),
    )
    with pytest.raises(RuntimeError, match="_session_owners"):
        McpSessionLimitMiddleware(
            app,
            session_manager=manager,
            max_sessions=1,
        )


@pytest.mark.asyncio
async def test_stateless_and_headerless_responses_do_not_leak_reservations():
    messages: list[Message] = []

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Message) -> None:
        messages.append(message)

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await send(
            {"type": "http.response.start", "status": 400, "headers": []}
        )
        await send({"type": "http.response.body", "body": b""})

    stateless = McpSessionLimitMiddleware(
        app,
        session_manager=SimpleNamespace(stateless=True),
        max_sessions=1,
    )
    scope: Scope = {
        "type": "http",
        "path": "/mcp",
        "method": "POST",
        "headers": [],
    }
    await stateless(scope, receive, send)

    stateful = McpSessionLimitMiddleware(
        app,
        session_manager=SimpleNamespace(
            stateless=False,
            _server_instances={},
            _session_owners=None,
        ),
        max_sessions=1,
    )
    await stateful(scope, receive, send)

    assert stateful._pending_creations == 0
    assert [
        message["status"]
        for message in messages
        if message["type"] == "http.response.start"
    ] == [400, 400]


@pytest.mark.asyncio
async def test_pending_initialize_reserves_capacity_without_holding_lock():
    entered = asyncio.Event()
    release = asyncio.Event()
    responses: list[list[Message]] = [[], []]

    async def send_first(message: Message) -> None:
        responses[0].append(message)

    async def send_second(message: Message) -> None:
        responses[1].append(message)

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        entered.set()
        await release.wait()
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"mcp-session-id", b"created")],
            }
        )
        await send({"type": "http.response.body", "body": b""})

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    manager = SimpleNamespace(
        stateless=False,
        _server_instances={},
        _session_owners={},
    )
    limiter = McpSessionLimitMiddleware(
        app,
        session_manager=manager,
        max_sessions=1,
    )
    scope: Scope = {
        "type": "http",
        "path": "/mcp",
        "method": "POST",
        "headers": [],
    }

    first = asyncio.create_task(limiter(scope, receive, send_first))
    await entered.wait()
    await limiter(scope, receive, send_second)
    release.set()
    await first

    assert responses[1][0]["status"] == 429
    assert limiter._pending_creations == 0


@pytest.mark.asyncio
async def test_cancelled_initialize_finishes_contended_reservation_release(
    monkeypatch,
):
    controls: asyncio.Queue[tuple[asyncio.Event, asyncio.Event]] = (
        asyncio.Queue()
    )
    release_started: asyncio.Queue[None] = asyncio.Queue()

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        del scope, receive
        entered, respond = await controls.get()
        entered.set()
        await respond.wait()
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"mcp-session-id", b"created")],
            }
        )

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(_message: Message) -> None:
        return None

    manager = SimpleNamespace(
        stateless=False,
        _server_instances={},
        _session_owners={},
    )
    limiter = McpSessionLimitMiddleware(
        app,
        session_manager=manager,
        max_sessions=1,
    )
    original_release = limiter._release_reservation

    async def observed_release() -> None:
        await release_started.put(None)
        await original_release()

    monkeypatch.setattr(limiter, "_release_reservation", observed_release)
    scope: Scope = {
        "type": "http",
        "path": "/mcp",
        "method": "POST",
        "headers": [],
    }

    for _ in range(3):
        entered = asyncio.Event()
        respond = asyncio.Event()
        await controls.put((entered, respond))
        request = asyncio.create_task(limiter(scope, receive, send))
        await entered.wait()
        await limiter._creation_lock.acquire()
        respond.set()
        await release_started.get()

        request.cancel()
        await asyncio.sleep(0)
        assert not request.done()
        limiter._creation_lock.release()
        with pytest.raises(asyncio.CancelledError):
            await request
        assert limiter._pending_creations == 0

    entered = asyncio.Event()
    respond = asyncio.Event()
    await controls.put((entered, respond))
    final_request = asyncio.create_task(limiter(scope, receive, send))
    await entered.wait()
    respond.set()
    await final_request
    assert limiter._pending_creations == 0
