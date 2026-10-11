import asyncio
import json
import time
from urllib.parse import parse_qs, urlsplit

import httpx2 as httpx
import pytest
from mcp.client.auth import AuthorizationCodeResult
from mcp.shared.auth import (
    OAuthClientInformationFull,
    OAuthMetadata,
    OAuthToken,
)

import workgate.agent_bridge.cli as agent_cli
from workgate.agent_bridge.auth import build_stored_oauth_provider
from workgate.agent_bridge.auth_store import AgentAuthStore
from workgate.agent_bridge.cli import revoke_stored_oauth
from workgate.agent_bridge.models import AgentMcpServerConfig
from workgate.utils.private_files import atomic_write_private_text

_SERVER_URL = "https://mcp.test/mcp"
_AUTH_URL = "https://auth.test"


def _server() -> AgentMcpServerConfig:
    return AgentMcpServerConfig.model_validate(
        {
            "type": "http",
            "url": _SERVER_URL,
            "auth": {"mode": "oauth", "scopes": ["tools.read"]},
        }
    )


def _oauth_metadata() -> OAuthMetadata:
    return OAuthMetadata.model_validate(
        {
            "issuer": _AUTH_URL,
            "authorization_endpoint": f"{_AUTH_URL}/authorize",
            "token_endpoint": f"{_AUTH_URL}/token",
            "registration_endpoint": f"{_AUTH_URL}/register",
            "revocation_endpoint": f"{_AUTH_URL}/revoke",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "token_endpoint_auth_methods_supported": ["none"],
            "code_challenge_methods_supported": ["S256"],
            "scopes_supported": ["tools.read"],
        }
    )


def _protected_resource() -> dict[str, object]:
    return {"resource": _SERVER_URL, "authorization_servers": [_AUTH_URL]}


def _client_info() -> OAuthClientInformationFull:
    return OAuthClientInformationFull.model_validate(
        {
            "client_id": "client-1",
            "redirect_uris": ["http://127.0.0.1/callback"],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        }
    )


@pytest.mark.asyncio
async def test_sdk_oauth_flow_uses_discovery_dcr_pkce_and_persists_tokens(
    tmp_path,
):
    store = AgentAuthStore(tmp_path / "agent_auth")
    redirects = []
    state_holder = {}
    requests = []

    async def redirect_handler(url: str) -> None:
        redirects.append(url)
        query = parse_qs(urlsplit(url).query)
        state_holder["state"] = query["state"][0]
        state_holder["challenge"] = query["code_challenge"][0]
        assert query["code_challenge_method"] == ["S256"]
        assert query["scope"] == ["tools.read"]
        assert "code_verifier" not in query

    async def callback_handler() -> AuthorizationCodeResult:
        return AuthorizationCodeResult(
            code="authorization-code", state=state_holder["state"], iss=None
        )

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(
            (request.method, str(request.url), request.headers, request.content)
        )
        url = str(request.url)
        if url == _SERVER_URL:
            if request.headers.get("Authorization") == "Bearer access-one":
                return httpx.Response(200, json={"jsonrpc": "2.0"})
            return httpx.Response(
                401,
                headers={
                    "WWW-Authenticate": (
                        'Bearer resource_metadata="'
                        "https://mcp.test/.well-known/oauth-protected-resource"
                        '" scope="tools.read"'
                    )
                },
            )
        if "oauth-protected-resource" in url:
            return httpx.Response(200, json=_protected_resource())
        if "oauth-authorization-server" in url or "openid-configuration" in url:
            return httpx.Response(
                200,
                json=_oauth_metadata().model_dump(
                    mode="json", exclude_none=True
                ),
            )
        if url == f"{_AUTH_URL}/register":
            registered = json.loads(request.content)
            assert registered["redirect_uris"] == ["http://127.0.0.1/callback"]
            return httpx.Response(
                201,
                json=_client_info().model_dump(mode="json", exclude_none=True),
            )
        if url == f"{_AUTH_URL}/token":
            form = parse_qs(request.content.decode())
            assert form["grant_type"] == ["authorization_code"]
            assert form["code"] == ["authorization-code"]
            assert form["code_verifier"][0]
            assert form["code_verifier"][0] != state_holder["challenge"]
            return httpx.Response(
                200,
                json={
                    "access_token": "access-one",
                    "refresh_token": "refresh-one",
                    "token_type": "Bearer",
                    "expires_in": 60,
                    "scope": "tools.read",
                },
            )
        return httpx.Response(404)

    provider = build_stored_oauth_provider(
        store,
        "docs",
        _server(),
        redirect_uri="http://127.0.0.1/callback",
        redirect_handler=redirect_handler,
        callback_handler=callback_handler,
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), auth=provider
    ) as client:
        response = await client.get(_SERVER_URL)

    assert response.status_code == 200
    assert len(redirects) == 1
    tokens = store.get_tokens("docs")
    client_info = store.get_client_info("docs")
    metadata = store.get_authorization_metadata("docs")
    assert tokens is not None and tokens.access_token == "access-one"
    assert tokens.refresh_token == "refresh-one"
    assert client_info is not None and client_info.client_id == "client-1"
    assert metadata is not None
    assert str(metadata.token_endpoint) == f"{_AUTH_URL}/token"
    assert any(url == f"{_AUTH_URL}/register" for _, url, _, _ in requests)


@pytest.mark.asyncio
async def test_oauth_refresh_after_restart_uses_persisted_authorization_metadata(
    tmp_path,
):
    store = AgentAuthStore(tmp_path / "agent_auth")
    store.set_client_info("docs", _client_info())
    store.set_authorization_metadata("docs", _oauth_metadata())
    store.set_tokens(
        "docs",
        OAuthToken.model_validate(
            {
                "access_token": "expired-access",
                "refresh_token": "refresh-one",
                "token_type": "Bearer",
                "expires_in": 60,
                "scope": "tools.read",
            }
        ),
    )
    payload = json.loads(store.path.read_text(encoding="utf-8"))
    payload["servers"]["docs"]["oauth"]["expires_at"] = time.time() - 1
    atomic_write_private_text(store.path, json.dumps(payload))
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, str(request.url), request.content))
        if str(request.url) == f"{_AUTH_URL}/token":
            form = parse_qs(request.content.decode())
            assert form["grant_type"] == ["refresh_token"]
            assert form["refresh_token"] == ["refresh-one"]
            return httpx.Response(
                200,
                json={
                    "access_token": "access-two",
                    "token_type": "Bearer",
                    "expires_in": 120,
                    "scope": "tools.read",
                },
            )
        if str(request.url) == _SERVER_URL:
            assert request.headers["Authorization"] == "Bearer access-two"
            return httpx.Response(200)
        return httpx.Response(404)

    provider = build_stored_oauth_provider(store, "docs", _server())
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), auth=provider
    ) as client:
        response = await client.get(_SERVER_URL)

    assert response.status_code == 200
    assert requests[0][1] == f"{_AUTH_URL}/token"
    tokens = store.get_tokens("docs")
    assert tokens is not None
    assert tokens.access_token == "access-two"
    assert tokens.refresh_token == "refresh-one"


@pytest.mark.asyncio
async def test_logout_revokes_refresh_token_when_endpoint_is_advertised(
    monkeypatch, tmp_path
):
    store = AgentAuthStore(tmp_path / "agent_auth")
    store.set_client_info("docs", _client_info())
    store.set_tokens(
        "docs",
        OAuthToken.model_validate(
            {
                "access_token": "access-one",
                "refresh_token": "refresh-one",
                "token_type": "Bearer",
            }
        ),
    )
    revoked = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == _SERVER_URL:
            return httpx.Response(
                401,
                headers={
                    "WWW-Authenticate": (
                        'Bearer resource_metadata="'
                        "https://mcp.test/.well-known/oauth-protected-resource"
                        '"'
                    )
                },
            )
        if "oauth-protected-resource" in url:
            return httpx.Response(200, json=_protected_resource())
        if "oauth-authorization-server" in url or "openid-configuration" in url:
            return httpx.Response(
                200,
                json=_oauth_metadata().model_dump(
                    mode="json", exclude_none=True
                ),
            )
        if url == f"{_AUTH_URL}/revoke":
            form = parse_qs(request.content.decode())
            revoked.append(form)
            return httpx.Response(200)
        return httpx.Response(404)

    original_client = httpx.AsyncClient

    def client_factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return original_client(*args, **kwargs)

    monkeypatch.setattr(agent_cli.httpx2, "AsyncClient", client_factory)

    result = await revoke_stored_oauth(store, "docs", _server())

    assert result.status == "revoked"
    assert revoked == [
        {
            "token": ["refresh-one"],
            "token_type_hint": ["refresh_token"],
            "client_id": ["client-1"],
        }
    ]


@pytest.mark.asyncio
async def test_noninteractive_runtime_fails_fast_when_reauthorization_is_required(
    tmp_path,
):
    store = AgentAuthStore(tmp_path / "agent_auth")
    store.set_client_info("docs", _client_info())
    store.set_authorization_metadata("docs", _oauth_metadata())
    store.set_tokens(
        "docs",
        OAuthToken.model_validate(
            {"access_token": "rejected", "token_type": "Bearer"}
        ),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == _SERVER_URL:
            return httpx.Response(401)
        return httpx.Response(404)

    provider = build_stored_oauth_provider(store, "docs", _server())
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), auth=provider
    ) as client:
        with pytest.raises(RuntimeError, match="workgate mcp auth docs"):
            await client.get(_SERVER_URL)


@pytest.mark.asyncio
async def test_failed_refresh_clears_stale_tokens_and_requires_cli_auth(
    tmp_path,
):
    store = AgentAuthStore(tmp_path / "agent_auth")
    store.set_client_info("docs", _client_info())
    store.set_authorization_metadata("docs", _oauth_metadata())
    store.set_tokens(
        "docs",
        OAuthToken.model_validate(
            {
                "access_token": "expired-access",
                "refresh_token": "bad-refresh",
                "token_type": "Bearer",
                "expires_in": 60,
            }
        ),
    )
    payload = json.loads(store.path.read_text(encoding="utf-8"))
    payload["servers"]["docs"]["oauth"]["expires_at"] = time.time() - 1
    atomic_write_private_text(store.path, json.dumps(payload))

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == f"{_AUTH_URL}/token":
            return httpx.Response(400, json={"error": "invalid_grant"})
        return httpx.Response(401)

    provider = build_stored_oauth_provider(store, "docs", _server())
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), auth=provider
    ) as client:
        with pytest.raises(RuntimeError, match="workgate mcp auth docs"):
            await client.get(_SERVER_URL)

    assert store.get_tokens("docs") is None
    assert store.get_client_info("docs") is not None
    assert store.get_authorization_metadata("docs") is not None


@pytest.mark.asyncio
async def test_loopback_callback_rejects_wrong_path_error_and_missing_code():
    async def send(callback, target: str):
        parsed = urlsplit(callback.redirect_uri)
        reader, writer = await asyncio.open_connection(
            parsed.hostname, parsed.port
        )
        writer.write(
            f"GET {target} HTTP/1.1\r\nHost: localhost\r\n\r\n".encode()
        )
        await writer.drain()
        response = await reader.read()
        writer.close()
        await writer.wait_closed()
        return response

    for target, match in [
        ("/wrong?code=x", "unexpected callback path"),
        ("/callback?error=denied&error_description=nope", "nope"),
        ("/callback?state=x", "omitted authorization code"),
    ]:
        async with agent_cli.LoopbackOAuthCallback() as callback:
            response = await send(callback, target)
            with pytest.raises(ValueError, match=match):
                await callback.wait()
            assert b"400 Bad Request" in response

    callback = agent_cli.LoopbackOAuthCallback()
    with pytest.raises(RuntimeError, match="has not started"):
        await callback.wait()


@pytest.mark.asyncio
async def test_authorize_server_rejects_non_oauth_and_reports_browser_failure(
    monkeypatch, tmp_path, capsys
):
    settings = agent_cli.Settings(state_dir=tmp_path / "state")
    non_oauth = AgentMcpServerConfig.model_validate(
        {"type": "http", "url": _SERVER_URL}
    )
    with pytest.raises(ValueError, match="not configured for OAuth"):
        await agent_cli.authorize_server(
            settings, "docs", non_oauth, no_open=True
        )

    oauth_server = _server()
    captured = {}

    class FakeCallback:
        redirect_uri = "http://127.0.0.1:1234/callback"

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def wait(self):
            return "code", "state"

    def fake_provider(store, server_name, server, **kwargs):
        captured["redirect_handler"] = kwargs["redirect_handler"]
        store.set_tokens(
            server_name,
            OAuthToken.model_validate(
                {"access_token": "fixture", "token_type": "Bearer"}
            ),
        )
        return object()

    class FakeManager:
        def __init__(self, *_args, **_kwargs):
            pass

        async def list_tools(self, _name, _server):
            await captured["redirect_handler"]("https://auth.test/authorize")
            return [object(), object()]

    monkeypatch.setattr(agent_cli, "LoopbackOAuthCallback", FakeCallback)
    monkeypatch.setattr(agent_cli, "build_stored_oauth_provider", fake_provider)
    monkeypatch.setattr(agent_cli, "AgentMcpClientManager", FakeManager)
    monkeypatch.setattr(agent_cli.webbrowser, "open", lambda _url: False)

    result = await agent_cli.authorize_server(
        settings, "docs", oauth_server, no_open=False
    )

    captured_output = capsys.readouterr()
    assert "Authorize docs:" in captured_output.out
    assert "Browser did not open" in captured_output.err
    assert result["authorized"] is True
    assert result["tool_count"] == 2


@pytest.mark.asyncio
async def test_revoke_oauth_covers_absent_unsupported_http_failure_and_exception(
    monkeypatch, tmp_path
):
    store = AgentAuthStore(tmp_path / "agent_auth")
    assert (
        await revoke_stored_oauth(store, "docs", _server())
    ).status == "not_authorized"

    store.set_tokens(
        "docs",
        OAuthToken.model_validate(
            {"access_token": "fixture", "token_type": "Bearer"}
        ),
    )

    class FakeClient:
        def __init__(self, outcome):
            self.outcome = outcome

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, *_args, **_kwargs):
            if isinstance(self.outcome, Exception):
                raise self.outcome
            return httpx.Response(self.outcome)

    async def no_metadata(_client, _url):
        return None

    monkeypatch.setattr(agent_cli, "_discover_oauth_metadata", no_metadata)
    monkeypatch.setattr(
        agent_cli.httpx2, "AsyncClient", lambda **_kwargs: FakeClient(200)
    )
    unsupported = await revoke_stored_oauth(store, "docs", _server())
    assert unsupported.status == "unsupported"

    async def metadata(_client, _url):
        return _oauth_metadata()

    monkeypatch.setattr(agent_cli, "_discover_oauth_metadata", metadata)
    monkeypatch.setattr(
        agent_cli.httpx2, "AsyncClient", lambda **_kwargs: FakeClient(500)
    )
    failed = await revoke_stored_oauth(store, "docs", _server())
    assert failed.status == "failed"
    assert failed.detail is not None
    assert "HTTP 500" in failed.detail

    monkeypatch.setattr(
        agent_cli.httpx2,
        "AsyncClient",
        lambda **_kwargs: FakeClient(RuntimeError("network fixture")),
    )
    errored = await revoke_stored_oauth(store, "docs", _server())
    assert errored.status == "failed"
    assert errored.detail is not None
    assert "RuntimeError" in errored.detail
