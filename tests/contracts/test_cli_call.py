"""An authenticated MCP CLI should not create another Control or auth bypass."""

import json
import os
from pathlib import Path

import pytest
from mcp.types import CallToolResult, TextContent

from workgate.cli import call
from workgate.main import _build_parser


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://localhost:8765", "http://localhost:8765/mcp"),
        ("https://127.0.0.1/mcp", "https://127.0.0.1/mcp"),
        ("http://[::1]:8765/mcp", "http://[::1]:8765/mcp"),
        ("http://127.0.0.1/", "http://127.0.0.1/mcp"),
    ],
)
def test_only_local_urls_are_normalized(url, expected):
    assert call._mcp_url(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "https://workgate.example/mcp",
        "http://localhost.evil.example/mcp",
        "http://127.0.0.1@evil.example/mcp",
        "http://x:secret@localhost:8765/mcp",
        "http://localhost%2eevil.example/mcp",
        "http://localhost:bad/mcp",
        "http://localhost:99999/mcp",
        "http://localhost:/mcp",
        "http://localhost:0/mcp",
        "http://localhost:8765/mcp?secret=x",
        "http://localhost:8765/mcp#fragment",
        "http://localhost:8765/tools",
        "file:///tmp/workgate.sock",
        "http:////127.0.0.1:8765/mcp",
        "http://[invalid]/mcp",
    ],
)
def test_url_never_accepts_untrusted_or_ambiguous_authority(url):
    with pytest.raises(ValueError):
        call._mcp_url(url)


def test_json_args_and_session_binding(monkeypatch):
    assert call._arguments('{"path":"a b"}', "session_1") == {
        "path": "a b",
        "session_id": "session_1",
    }
    assert call._arguments("{}", None) == {}
    for value in ("[]", '"hello"', '{"x": NaN}', '{"x": Infinity}', "{"):
        with pytest.raises(ValueError):
            call._arguments(value, None)
    with pytest.raises(ValueError, match="conflicts"):
        call._arguments('{"session_id":"two"}', "one")
    with pytest.raises(ValueError, match="1 MiB"):
        call._arguments('"' + "a" * (1024 * 1024 + 1) + '"', None)

    class Stdin:
        def isatty(self):
            return False

        def read(self, _size):
            return '{"from":"stdin"}'

    monkeypatch.setattr(call.sys, "stdin", Stdin())
    assert call._arguments(None, None) == {"from": "stdin"}
    monkeypatch.setattr(Stdin, "read", lambda self, _size: "")
    assert call._arguments(None, None) == {}


def test_token_file_requires_owner_private_mode(tmp_path: Path, monkeypatch):
    token_file = tmp_path / "access-token"
    token_file.write_text("testing-token\n", encoding="utf-8")
    if os.name != "nt":
        token_file.chmod(0o644)
        with pytest.raises(ValueError, match="readable"):
            call._read_token(str(token_file))
    token_file.chmod(0o600)
    assert call._read_token(str(token_file)) == "testing-token"
    token_file.write_text("token\nsecond-line", encoding="utf-8")
    with pytest.raises(ValueError, match="one OAuth access token"):
        call._read_token(str(token_file))
    link = tmp_path / "symlink"
    try:
        link.symlink_to(token_file)
    except OSError, NotImplementedError:
        return
    with pytest.raises(ValueError, match="not a symlink"):
        call._read_token(str(link))


def test_token_env_and_file_precedence(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKGATE_CALL_TOKEN", "env-token")
    assert call._read_token(None) == "env-token"
    token = tmp_path / "token"
    token.write_text("file-token", encoding="utf-8")
    token.chmod(0o600)
    assert call._read_token(str(token)) == "file-token"


def test_cli_registration_and_no_direct_bypass():
    parser = _build_parser()
    args = parser.parse_args(["call", "session_start", "--json", "{}"])
    assert (
        args.tool == "session_start" and args.handler is call.run_call_from_args
    )
    with pytest.raises(SystemExit):
        parser.parse_args(["call", "session_start", "--direct"])


@pytest.mark.parametrize("is_error", [True, False])
def test_cli_formats_structured_result_and_exit_code(
    monkeypatch, capsys, is_error
):
    received = []

    async def fake_call(url, tool, arguments, token, timeout_s):
        received.append((url, tool, arguments, token, timeout_s))
        return CallToolResult(
            content=[TextContent(type="text", text="tool output")],
            structured_content={"answer": 42},
            is_error=is_error,
        )

    monkeypatch.setattr(call, "_call", fake_call)
    args = _build_parser().parse_args(
        ["call", "session_start", "--json", "{}", "--session", "session_1"]
    )
    if is_error:
        with pytest.raises(SystemExit) as error:
            args.handler(args)
        assert error.value.code == 1
    else:
        args.handler(args)
    result = json.loads(capsys.readouterr().out)
    if is_error:
        assert result["isError"] is True
    else:
        assert result == {"answer": 42}
    assert received[0][1:3] == (
        "session_start",
        {"session_id": "session_1"},
    )


def test_unauthorized_or_transport_errors_do_not_echo_token(
    monkeypatch, capsys
):
    async def raise_secret(*_args):
        raise RuntimeError("Authorization: Bearer super-secret")

    monkeypatch.setattr(call, "_call", raise_secret)
    monkeypatch.setenv("WORKGATE_CALL_TOKEN", "super-secret")
    args = _build_parser().parse_args(["call", "session_start", "--json", "{}"])
    with pytest.raises(SystemExit) as error:
        args.handler(args)
    assert error.value.code == 1
    captured = capsys.readouterr()
    assert not captured.out
    assert "super-secret" not in captured.err
    assert "Authorization" not in captured.err


@pytest.mark.asyncio
async def test_http_client_uses_bearer_without_redirects_or_environment_proxy(
    monkeypatch,
):
    from contextlib import asynccontextmanager

    params = {}

    @asynccontextmanager
    async def fake_client(*_args, **kwargs):
        params.update(kwargs)
        yield object()

    @asynccontextmanager
    async def fake_transport(url, *, http_client):
        assert url == "http://127.0.0.1:8765/mcp"
        yield "read", "write"

    class FakeSession:
        def __init__(self, read, write):
            assert (read, write) == ("read", "write")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def initialize(self):
            return None

        async def call_tool(self, name, args, *, read_timeout_seconds):
            assert name == "session_start"
            assert args == {"workdir": "."}
            assert read_timeout_seconds == 9
            return CallToolResult(
                content=[TextContent(type="text", text="ok")], is_error=False
            )

    monkeypatch.setattr(call.httpx2, "AsyncClient", fake_client)
    monkeypatch.setattr(call, "streamable_http_client", fake_transport)
    monkeypatch.setattr(call, "ClientSession", FakeSession)
    outcome = await call._call(
        "http://127.0.0.1:8765/mcp",
        "session_start",
        {"workdir": "."},
        "private-access-token",
        9,
    )
    assert outcome.is_error is False
    assert params["headers"] == {"Authorization": "Bearer private-access-token"}
    assert params["follow_redirects"] is False
    assert params["trust_env"] is False


def test_cli_outputs_native_image_blocks_instead_of_metadata_only(
    monkeypatch, capsys
):
    from mcp.types import ImageContent

    async def fake_call(*_args):
        return CallToolResult(
            content=[
                ImageContent(
                    type="image", data="aGVsbG8=", mime_type="image/png"
                )
            ],
            structured_content={"bytes": 5},
            is_error=False,
        )

    monkeypatch.setattr(call, "_call", fake_call)
    args = _build_parser().parse_args(["call", "view_image", "--json", "{}"])
    args.handler(args)
    output = json.loads(capsys.readouterr().out)
    assert output["content"][0]["mimeType"] == "image/png"
    assert output["content"][0]["data"] == "aGVsbG8="
    assert output["structuredContent"] == {"bytes": 5}


def test_explicit_token_validation_rejects_header_newlines(monkeypatch):
    monkeypatch.setenv("WORKGATE_CALL_TOKEN", "valid-token\nInjected: header")
    with pytest.raises(ValueError, match="OAuth access token"):
        call._read_token(None)
