import json
from typing import Any, cast

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient
from starlette.types import ASGIApp, Message, Scope

import workgate.http.request_limits as request_limit_module
from workgate.config.settings import (
    Settings,
    clear_settings_cache,
    configure_settings,
)
from workgate.control.http.app import build_http_app
from workgate.control.mcp.app import build_mcp_http_app
from workgate.http.request_limits import (
    RequestBodyLimitMiddleware,
)


@pytest.fixture(autouse=True)
def _reset_settings_after_test():
    yield
    clear_settings_cache()


async def _echo_body(request: Request) -> JSONResponse:
    body = await request.body()
    return JSONResponse({"body": body.decode("utf-8"), "bytes": len(body)})


class _DummyMcp:
    def streamable_http_app(self) -> Starlette:
        return Starlette(routes=[Route("/mcp", _echo_body, methods=["POST"])])


def _scope(
    *,
    path: str = "/upload",
    headers: tuple[tuple[bytes, bytes], ...] = (),
) -> Scope:
    return cast(
        Scope,
        {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode("ascii"),
            "query_string": b"",
            "headers": list(headers),
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 80),
            "root_path": "",
        },
    )


async def _invoke(
    app: ASGIApp,
    messages: list[Message],
    *,
    scope: Scope | None = None,
) -> tuple[list[Message], int]:
    received = 0
    sent: list[Message] = []

    async def receive() -> Message:
        nonlocal received
        received += 1
        if messages:
            return messages.pop(0)
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        sent.append(message)

    await app(scope or _scope(), receive, send)
    return sent, received


def _response(
    sent: list[Message],
) -> tuple[int, dict[str, str], dict[str, Any]]:
    start = next(
        message for message in sent if message["type"] == "http.response.start"
    )
    body = b"".join(
        cast(bytes, message.get("body", b""))
        for message in sent
        if message["type"] == "http.response.body"
    )
    headers = {
        cast(bytes, name).decode("latin-1"): cast(bytes, value).decode(
            "latin-1"
        )
        for name, value in cast(list[tuple[bytes, bytes]], start["headers"])
    }
    return int(start["status"]), headers, json.loads(body)


@pytest.mark.asyncio
async def test_declared_oversize_rejects_without_reading_body(monkeypatch):
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        request_limit_module,
        "audit",
        lambda event, **fields: calls.append({"event": event, **fields}),
    )

    async def downstream(scope, receive, send):
        raise AssertionError("oversized declared body reached downstream")

    app = RequestBodyLimitMiddleware(downstream, max_bytes=8)
    sent, received = await _invoke(
        app,
        [{"type": "http.request", "body": b"ignored", "more_body": False}],
        scope=_scope(headers=((b"content-length", b"9"),)),
    )
    status, headers, payload = _response(sent)

    assert received == 0
    assert status == 413
    assert headers["cache-control"] == "no-store"
    assert payload == {
        "error": "request_too_large",
        "message": "Request body exceeds the configured 8 byte limit",
        "limit_bytes": 8,
    }
    assert calls == [
        {
            "event": "http_request_rejected",
            "reason": "request_body_too_large",
            "path": "/upload",
            "method": "POST",
            "limit_bytes": 8,
            "observed_bytes": None,
            "declared_bytes": 9,
        }
    ]


@pytest.mark.asyncio
async def test_chunked_oversize_reports_exact_observed_bytes(monkeypatch):
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        request_limit_module,
        "audit",
        lambda event, **fields: calls.append({"event": event, **fields}),
    )
    downstream_called = False

    async def downstream(scope, receive, send):
        nonlocal downstream_called
        downstream_called = True

    app = RequestBodyLimitMiddleware(downstream, max_bytes=8)
    sent, received = await _invoke(
        app,
        [
            {"type": "http.request", "body": b"abcd", "more_body": True},
            {"type": "http.request", "body": b"efghi", "more_body": False},
        ],
    )
    status, _headers, payload = _response(sent)

    assert downstream_called is False
    assert received == 2
    assert status == 413
    assert payload["limit_bytes"] == 8
    assert calls[0]["observed_bytes"] == 9
    assert calls[0]["declared_bytes"] is None


@pytest.mark.asyncio
async def test_exact_limit_replays_original_chunks_to_downstream():
    app = RequestBodyLimitMiddleware(
        Starlette(routes=[Route("/upload", _echo_body, methods=["POST"])]),
        max_bytes=8,
    )
    sent, received = await _invoke(
        app,
        [
            {"type": "http.request", "body": b"abc", "more_body": True},
            {"type": "http.request", "body": b"defgh", "more_body": False},
        ],
    )
    status, _headers, payload = _response(sent)

    assert received == 2
    assert status == 200
    assert payload == {"body": "abcdefgh", "bytes": 8}


@pytest.mark.asyncio
async def test_actual_body_wins_over_misleading_small_content_length():
    app = RequestBodyLimitMiddleware(
        Starlette(routes=[Route("/upload", _echo_body, methods=["POST"])]),
        max_bytes=8,
    )
    sent, _received = await _invoke(
        app,
        [
            {"type": "http.request", "body": b"12345", "more_body": True},
            {"type": "http.request", "body": b"6789", "more_body": False},
        ],
        scope=_scope(headers=((b"content-length", b"4"),)),
    )
    status, _headers, payload = _response(sent)

    assert status == 413
    assert payload["limit_bytes"] == 8


@pytest.mark.asyncio
async def test_zero_limit_disables_body_middleware():
    app = RequestBodyLimitMiddleware(
        Starlette(routes=[Route("/upload", _echo_body, methods=["POST"])]),
        max_bytes=0,
    )
    sent, _received = await _invoke(
        app,
        [{"type": "http.request", "body": b"unbounded", "more_body": False}],
    )
    status, _headers, payload = _response(sent)

    assert status == 200
    assert payload == {"body": "unbounded", "bytes": 9}


def _configure_http_limit(
    tmp_path,
    monkeypatch,
    *,
    auth_mode: str = "none",
    remote_enabled: bool = True,
) -> None:
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / ".state"))
    monkeypatch.setenv("WORKGATE_AUTH_MODE", auth_mode)
    monkeypatch.setenv("WORKGATE_REMOTE_ENABLED", str(remote_enabled).lower())
    monkeypatch.setenv("WORKGATE_MAX_HTTP_REQUEST_BYTES", "64")
    monkeypatch.setenv("WORKGATE_AGENT_BRIDGE_ENABLED", "false")
    clear_settings_cache()


def _assert_oversize(response) -> None:
    assert response.status_code == 413
    assert response.json() == {
        "error": "request_too_large",
        "message": "Request body exceeds the configured 64 byte limit",
        "limit_bytes": 64,
    }
    assert response.headers["cache-control"] == "no-store"


def test_rest_tool_body_limit_precedes_fastapi_parsing(tmp_path, monkeypatch):
    _configure_http_limit(tmp_path, monkeypatch)

    response = TestClient(build_http_app()).post(
        "/tools/session_start",
        content=b"x" * 65,
        headers={"content-type": "application/json"},
    )

    _assert_oversize(response)


def test_oauth_form_body_uses_shared_limit(tmp_path, monkeypatch):
    _configure_http_limit(tmp_path, monkeypatch)

    response = TestClient(build_http_app()).post(
        "/oauth/token",
        content=b"grant_type=authorization_code&code=" + b"x" * 64,
        headers={"content-type": "application/x-www-form-urlencoded"},
    )

    _assert_oversize(response)


@pytest.mark.parametrize("path", ["/mcp", "/remote/register"])
def test_mcp_and_remote_public_routes_use_shared_limit(
    tmp_path, monkeypatch, path
):
    _configure_http_limit(tmp_path, monkeypatch)
    configure_settings(
        Settings(
            workspace_root=tmp_path,
            state_dir=tmp_path / ".state",
            auth_mode="none",
            remote_enabled=True,
            max_http_request_bytes=64,
            agent_bridge_enabled=False,
        )
    )
    app = build_mcp_http_app(cast(Any, _DummyMcp()))

    with TestClient(app) as client:
        response = client.post(
            path,
            content=b"x" * 65,
            headers={"content-type": "application/json"},
        )

    _assert_oversize(response)


def test_protected_mcp_route_authenticates_before_reading_large_body(
    tmp_path, monkeypatch
):
    _configure_http_limit(tmp_path, monkeypatch, auth_mode="oauth")
    configure_settings(
        Settings(
            workspace_root=tmp_path,
            state_dir=tmp_path / ".state",
            auth_mode="oauth",
            remote_enabled=False,
            max_http_request_bytes=64,
            base_url="https://workgate.example.com",
            agent_bridge_enabled=False,
        )
    )
    app = build_mcp_http_app(cast(Any, _DummyMcp()))

    with TestClient(app) as client:
        response = client.post(
            "/mcp",
            content=b"x" * 65,
            headers={"content-type": "application/json"},
        )

    assert response.status_code == 401
    assert "resource_metadata" in response.headers["www-authenticate"]


@pytest.mark.asyncio
async def test_audit_failure_does_not_break_413_response(monkeypatch):
    def fail_audit(*_args, **_kwargs):
        raise OSError("audit unavailable")

    monkeypatch.setattr(request_limit_module, "audit", fail_audit)
    app = RequestBodyLimitMiddleware(
        Starlette(routes=[Route("/upload", _echo_body, methods=["POST"])]),
        max_bytes=4,
    )

    sent, _received = await _invoke(
        app,
        [{"type": "http.request", "body": b"12345", "more_body": False}],
    )
    status, _headers, payload = _response(sent)

    assert status == 413
    assert payload["limit_bytes"] == 4


@pytest.mark.asyncio
async def test_replay_receive_delegates_after_buffered_messages():
    live_calls = 0

    async def live_receive() -> Message:
        nonlocal live_calls
        live_calls += 1
        return {"type": "http.disconnect"}

    request_message: Message = {
        "type": "http.request",
        "body": b"payload",
        "more_body": False,
    }
    buffered = request_limit_module._BufferedRequest(b"payload", False, 7)
    replay = RequestBodyLimitMiddleware._replay_receive(buffered, live_receive)

    assert await replay() == request_message
    assert await replay() == {"type": "http.disconnect"}
    assert live_calls == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_real_http_process_rejects_oversized_body(tmp_path, monkeypatch):
    import httpx

    from tests.e2e_helpers import run_http_process

    monkeypatch.setenv("WORKGATE_MAX_HTTP_REQUEST_BYTES", "64")
    async with (
        run_http_process(tmp_path, mode="http") as (base_url, _workspace),
        httpx.AsyncClient(timeout=10) as client,
    ):
        response = await client.post(
            f"{base_url}/tools/session_start",
            content=b"x" * 65,
            headers={"content-type": "application/json"},
        )

    _assert_oversize(response)
