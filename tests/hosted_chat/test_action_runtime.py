"""Pinned Action transport contract and real loopback HTTP regression tests."""

import asyncio
import base64
import json
import socket

import httpcore
import pytest

from workgate.hosted_chat import action_runtime as runtime
from workgate.hosted_chat.action_spec import operations


def operation(server, *, method="get", path="/echo/{item}"):
    spec = {
        "paths": {
            path: {
                method: {
                    "operationId": "echo",
                    "x-strict": True,
                    "parameters": [
                        {
                            "name": "item",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "string"},
                        },
                        {
                            "name": "limit",
                            "in": "query",
                            "schema": {
                                "type": "integer",
                                "minimum": 0,
                                "maximum": 10,
                            },
                        },
                        {
                            "name": "X-Label",
                            "in": "header",
                            "schema": {"type": "string"},
                        },
                    ],
                }
            }
        }
    }
    return {**operations(spec)["echo"], "server": server}


@pytest.mark.parametrize(
    "ips,allowed",
    [
        (["8.8.8.8", "1.1.1.1"], True),
        (["8.8.8.8", "127.0.0.1"], False),
        (["10.0.0.2"], False),
        (["::ffff:169.254.169.254"], False),
        (["100.100.100.200"], False),
        (["224.0.0.1"], False),
        ([], False),
    ],
)
def test_dns_public_only(monkeypatch, ips, allowed):
    monkeypatch.delenv("WORKGATE_ACTION_ALLOWED_ADDRESSES", raising=False)
    monkeypatch.delenv("WORKGATE_ACTION_ALLOWED_DOMAINS", raising=False)

    def resolve(host, port, **kwargs):
        return [
            (
                socket.AF_INET6 if ":" in ip else socket.AF_INET,
                socket.SOCK_STREAM,
                0,
                "",
                (ip, port),
            )
            for ip in ips
        ]

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    if allowed:
        assert runtime._resolve_addresses("example.org", 443) == ips
    else:
        with pytest.raises(httpcore.ConnectError):
            runtime._resolve_addresses("example.org", 443)


def test_exact_private_allowlist(monkeypatch):
    monkeypatch.setenv(
        "WORKGATE_ACTION_ALLOWED_ADDRESSES", "internal.example:443"
    )
    monkeypatch.setenv("WORKGATE_ACTION_ALLOWED_DOMAINS", "internal.example")
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **kw: [
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("10.0.0.2", 443))
        ],
    )
    assert runtime._resolve_addresses("internal.example", 443) == ["10.0.0.2"]
    with pytest.raises(httpcore.ConnectError):
        runtime._resolve_addresses("internal.example", 80)
    with pytest.raises(httpcore.ConnectError):
        runtime._resolve_addresses("other.example", 443)
    assert not runtime._allowed_ip("169.254.169.254", local=True)
    assert not runtime._allowed_ip("fe80::1%eth0", local=True)
    assert not runtime._allowed_ip("100.100.100.200", local=True)


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.org",
        "https://user:pass@example.org",
        "https://example.org/?a=b",
        "https://example.org/#x",
        "http://example.org:0",
        "https://example.org/\\bad",
    ],
)
def test_invalid_server(url):
    with pytest.raises(ValueError):
        runtime.ActionHTTPTransport(url)


def test_request_shape_and_validation():
    op = operation("https://example.org/v1")
    assert runtime._request(
        op, {"item": "a/b", "limit": 3, "X-Label": "ok"}
    ) == ("/echo/a%2Fb", {"limit": 3}, {}, {"X-Label": "ok"})
    assert (
        runtime._full_url(op["server"], "/echo/a")
        == "https://example.org/v1/echo/a"
    )
    for args in (
        {},
        {"item": 1},
        {"item": "a", "limit": True},
        {"item": "a", "limit": -1},
        {"item": "a", "limit": 11},
        {"item": "a", "extra": True},
        {"item": "a", "X-Label": "a\r\nb"},
    ):
        with pytest.raises(ValueError):
            runtime._request(op, args)
    op["locations"]["X-Label"] = "header"
    op["parameters"]["properties"]["Host"] = {"type": "string"}
    op["locations"]["Host"] = "header"
    with pytest.raises(ValueError, match="Reserved"):
        runtime._request(op, {"item": "a", "Host": "other"})
    with pytest.raises(ValueError):
        runtime._full_url(op["server"], "//elsewhere")
    with pytest.raises(ValueError):
        runtime._full_url(op["server"], "/x?y")


def test_nested_schema_and_scrubbing():
    schema = {"type": "array", "items": {"type": "boolean", "enum": [True]}}
    runtime._validate([True], schema)
    for value in (True, [1], [False]):
        with pytest.raises(ValueError):
            runtime._validate(value, schema)
    assert runtime._authorization(
        {"auth": {"type": "service_http", "authorization_type": "basic"}},
        {"api_key": "x"},
    ) == {"Authorization": "Basic eA=="}
    assert runtime._authorization(
        {
            "auth": {
                "type": "service_http",
                "authorization_type": "custom",
                "custom_auth_header": "X-Key",
            }
        },
        {"api_key": "x"},
    ) == {"X-Key": "x"}
    assert runtime._authorization(
        {"auth": {"type": "oauth"}}, {"oauth_access_token": "x"}
    ) == {"Authorization": "Bearer x"}
    with pytest.raises(PermissionError):
        runtime._authorization({"auth": {"type": "oauth"}}, {})
    secret = 'test-secret+"'
    assert runtime._scrub(secret, {"api_key": secret}) == "[redacted]"
    assert (
        runtime._scrub(
            base64.b64encode(secret.encode()).decode(), {"api_key": secret}
        )
        == "[redacted]"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode",
    [
        "success",
        "redirect",
        "large",
        "encoding",
        "timeout",
        "cancel",
        "empty",
        "text",
        "post",
        "denied",
    ],
)
async def test_real_http_action(monkeypatch, mode):
    requests = []
    started = asyncio.Event()
    finished = asyncio.Event()

    async def reply(reader, writer):
        try:
            headers = await reader.readuntil(b"\r\n\r\n")
            length = next(
                (
                    int(line.split(b":", 1)[1])
                    for line in headers.split(b"\r\n")
                    if line.lower().startswith(b"content-length:")
                ),
                0,
            )
            content = await reader.readexactly(length)
            requests.append((headers, content))
            started.set()
            if mode in {"timeout", "cancel"}:
                await reader.read()
                return
            status, extra, payload = (
                b"200 OK",
                b"",
                json.dumps({"echo": "test-action-secret"}).encode(),
            )
            if mode == "redirect":
                status, extra = (
                    b"302 Found",
                    b"Location: http://elsewhere.invalid/\r\n",
                )
            if mode == "large":
                payload = b"x" * 1025
            if mode == "encoding":
                extra = b"Content-Encoding: gzip\r\n"
            if mode == "empty":
                payload = b""
            if mode == "text":
                payload = b"not JSON test-action-secret"
            writer.write(
                b"HTTP/1.1 "
                + status
                + b"\r\n"
                + extra
                + b"Connection: close\r\nContent-Length: "
                + str(len(payload)).encode()
                + b"\r\n\r\n"
                + payload
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
            finished.set()

    server = await asyncio.start_server(reply, "127.0.0.1", 0)
    assert server.sockets
    port = server.sockets[0].getsockname()[1]
    monkeypatch.setenv(
        "WORKGATE_ACTION_ALLOWED_ADDRESSES",
        f"127.0.0.1:{port}" if mode != "denied" else "",
    )
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.delenv("WORKGATE_ACTION_ALLOWED_DOMAINS", raising=False)
    monkeypatch.setattr(runtime, "_MAX_RESPONSE", 1024)
    if mode == "timeout":
        monkeypatch.setattr(runtime, "_TIMEOUT_S", 0.15)
    op = operation(
        f"http://127.0.0.1:{port}/v1",
        method="post" if mode == "post" else "get",
    )
    meta = {"auth": {"type": "service_http", "authorization_type": "bearer"}}
    if mode == "denied":
        meta = {"auth": {"type": "none"}}
    async with server:
        task = asyncio.create_task(
            runtime.execute_action(
                meta,
                {"api_key": "test-action-secret"},
                op,
                {"item": "abc", "limit": 3},
            )
        )
        if mode == "cancel":
            await asyncio.wait_for(started.wait(), 5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            result = await task
            expected = {
                "success": '{"echo":"[redacted]"}',
                "post": '{"echo":"[redacted]"}',
                "redirect": "API call failed (HTTP 302)",
                "large": "API call failed (response too large)",
                "encoding": "API call failed (unsupported response encoding)",
                "timeout": "API call failed (timeout)",
                "empty": "",
                "text": "not JSON [redacted]",
                "denied": "API call failed (network error)",
            }
            assert result == expected[mode]
        if mode != "denied":
            await asyncio.wait_for(finished.wait(), 5)
            assert len(requests) == 1
            assert b"/v1/echo/abc?limit=3" in requests[0][0]
            assert b"Authorization: Bearer test-action-secret" in requests[0][0]
            assert b"Accept-Encoding: identity" in requests[0][0]
            if mode == "post":
                assert requests[0][1] == b"{}"
        else:
            assert not requests


@pytest.mark.asyncio
async def test_owned_invocation_binding(tmp_path, monkeypatch):
    from workgate.hosted_chat.actions import ActionRepository
    from workgate.hosted_chat.agents import AgentRepository
    from workgate.hosted_chat.database import SQLiteDatabase

    db = SQLiteDatabase(tmp_path)
    agent = AgentRepository(db).create(
        "alice", {"name": "Action Agent", "provider": "openAI", "model": None}
    )
    body = {
        "metadata": {
            "domain": "https://api.example.org",
            "raw_spec": json.dumps(
                {
                    "servers": [{"url": "https://api.example.org"}],
                    "paths": {"/ping": {"get": {"operationId": "ping"}}},
                }
            ),
            "auth": {"type": "none"},
        },
        "functions": [{"function": {"name": "ping"}}],
    }
    updated, saved = ActionRepository(db).save("alice", agent["id"], body)
    name = updated["tools"][0].replace("---", "_")
    calls = []

    async def execute(meta, creds, op, args):
        calls.append((meta, creds, op, args))
        return "ok"

    monkeypatch.setattr(runtime, "execute_action", execute)
    assert (
        await runtime.invoke_action(
            "alice",
            agent["id"],
            name,
            {},
            db,
            expected_version=saved["version"],
        )
        == "ok"
    )
    assert len(calls) == 1
    with pytest.raises(PermissionError):
        await runtime.invoke_action("bob", agent["id"], name, {}, db)
    with pytest.raises(PermissionError):
        await runtime.invoke_action(
            "alice", agent["id"], name, {}, db, expected_version=999
        )

    async def invalid(*args):
        raise ValueError("secret")

    monkeypatch.setattr(runtime, "execute_action", invalid)
    assert (
        await runtime.invoke_action("alice", agent["id"], name, {}, db)
        == "API call failed (ValueError)"
    )

    async def error(*args):
        raise RuntimeError("secret")

    monkeypatch.setattr(runtime, "execute_action", error)
    assert (
        await runtime.invoke_action("alice", agent["id"], name, {}, db)
        == "API call failed (execution error)"
    )


@pytest.mark.asyncio
async def test_transport_rejects_cleartext_credentials_and_unsupported_body(
    monkeypatch,
):
    monkeypatch.delenv("WORKGATE_ACTION_ALLOWED_ADDRESSES", raising=False)
    op = operation("http://example.org")
    meta = {"auth": {"type": "service_http", "authorization_type": "bearer"}}
    with pytest.raises(PermissionError, match="HTTPS"):
        await runtime.execute_action(
            meta, {"api_key": "secret"}, op, {"item": "a"}
        )
    op["server"] = "https://example.org"
    op["content_type"] = "text/plain"
    with pytest.raises(ValueError, match="content type"):
        await runtime.execute_action(
            meta, {"api_key": "secret"}, op, {"item": "a"}
        )
    op["content_type"] = ""
    meta = {
        "auth": {
            "type": "service_http",
            "authorization_type": "custom",
            "custom_auth_header": "x-label",
        }
    }
    with pytest.raises(ValueError, match="conflicts"):
        await runtime.execute_action(
            meta, {"api_key": "secret"}, op, {"item": "a", "X-Label": "b"}
        )
