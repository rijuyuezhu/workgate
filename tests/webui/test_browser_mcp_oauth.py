"""Browser relay security: one-use state, owner binding and private SDK persistence."""

import asyncio
from types import SimpleNamespace
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from fastapi import HTTPException
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.applications import Starlette
from starlette.requests import Request

import workgate.control.http.integration_oauth as ui
from workgate.agent_bridge.auth_store import AgentAuthStore
from workgate.agent_bridge.models import AgentMcpServerConfig
from workgate.oauth.core.context import bind_oauth_claims, reset_oauth_claims


def _request(app, query=""):
    return Request(
        {
            "type": "http",
            "method": "GET",
            "scheme": "https",
            "path": ui._CALLBACK,
            "query_string": query.encode(),
            "headers": [(b"host", b"control.test")],
            "server": ("control.test", 443),
            "app": app,
        }
    )


class _Subject:
    def __init__(self, sub):
        self.sub = sub

    def __enter__(self):
        self.token = bind_oauth_claims(
            {"sub": self.sub, "scope": "executor:use shell:write"}
        )
        return self

    def __exit__(self, *_):
        reset_oauth_claims(self.token)


@pytest.fixture
def relay(tmp_path, monkeypatch):
    app = Starlette()
    config = SimpleNamespace(
        agent_config_dir=tmp_path / "config",
        agent_auth_dir=tmp_path / "auth",
        resolved_base_url="https://control.test",
        agent_mcp_call_timeout_s=5,
    )
    server = AgentMcpServerConfig.model_validate(
        {
            "type": "http",
            "url": "https://mcp.test/mcp",
            "enabled": True,
            "auth": {"mode": "oauth", "scopes": ["tools.read"]},
        }
    )
    app.state.control_runtime = SimpleNamespace(
        config=config,
        agent_bridge=SimpleNamespace(invalidate_all_discovery=lambda: None),
    )
    monkeypatch.setattr(
        ui,
        "load_agent_manifest",
        lambda *_args, **_kwargs: SimpleNamespace(
            data=SimpleNamespace(mcp_servers={"docs": server})
        ),
    )

    class FakeProvider:
        def __init__(self, **kwargs):
            self.context = SimpleNamespace(
                oauth_metadata=SimpleNamespace(
                    authorization_endpoint="https://auth.test/authorize"
                ),
                redirect_handler=kwargs["redirect_handler"],
                callback_handler=kwargs["callback_handler"],
            )

    monkeypatch.setattr(
        ui,
        "build_stored_oauth_provider",
        lambda *_args, **kwargs: FakeProvider(**kwargs),
    )

    class FakeManager:
        def __init__(
            self, _timeout, store, *, oauth_provider_factory, allow_stdio
        ):
            self.store = store
            self.provider = oauth_provider_factory("docs", server)

        async def list_tools(self, _name, _server):
            params = {
                "redirect_uri": config.resolved_base_url + ui._CALLBACK,
                "code_challenge_method": "S256",
                "code_challenge": "a" * 43,
                "state": "state-" + str(id(self)),
            }
            await self.provider.context.redirect_handler(
                "https://auth.test/authorize?" + urlencode(params)
            )
            result = await self.provider.context.callback_handler()
            assert result.code == "approved"
            self.store.set_tokens(
                "docs",
                OAuthToken(
                    access_token="private-access-token",
                    token_type="Bearer",
                    scope="tools.read",
                ),
            )
            return []

    monkeypatch.setattr(ui, "AgentMcpClientManager", FakeManager)
    return ui.BrowserMcpOAuth(), app, config


@pytest.mark.asyncio
async def test_browser_oauth_callback_replay_and_wrong_user(relay):
    broker, app, config = relay
    with _Subject("alice"):
        started = await broker.begin(_request(app), "docs")
        sid = started["session_id"]
        query = parse_qs(urlsplit(started["authorization_url"]).query)
        assert query["code_challenge_method"] == ["S256"]
        assert query["redirect_uri"] == ["https://control.test" + ui._CALLBACK]
        state = query["state"][0]
        assert "private-access-token" not in str(started)

    assert broker.receive(
        _request(app, urlencode({"state": state, "code": "approved"}))
    )
    # Polling must not exchange tokens, even when repeated.
    with _Subject("alice"):
        for _ in range(3):
            assert broker.status(sid)["status"] == "awaiting_confirmation"
    assert AgentAuthStore(config.agent_auth_dir).get_tokens("docs") is None
    assert not broker.receive(
        _request(app, urlencode({"state": state, "code": "approved"}))
    )
    with _Subject("mallory"):
        with pytest.raises(HTTPException) as exc:
            broker.status(sid)
        assert exc.value.status_code == 404
        with pytest.raises(HTTPException) as exc:
            broker.complete(sid, config.agent_config_dir)
        assert exc.value.status_code == 404
    with _Subject("alice"):
        assert broker.status(sid)["status"] == "awaiting_confirmation"
        assert (
            broker.complete(sid, config.agent_config_dir)["status"]
            == "completing"
        )
    with _Subject("alice"), pytest.raises(HTTPException) as exc:
        broker.complete(sid, config.agent_config_dir)
    assert exc.value.status_code == 409  # Single use.
    await asyncio.sleep(0.1)
    with _Subject("alice"):
        assert broker.status(sid)["status"] == "authorized"
    tokens = AgentAuthStore(config.agent_auth_dir).get_tokens("docs")
    assert tokens is not None and tokens.access_token == "private-access-token"
    broker.cancel(sid)


@pytest.mark.asyncio
async def test_current_handshake_recovery_is_owner_bound_read_only_and_expires(
    relay,
):
    broker, app, config = relay
    with _Subject("alice"):
        started = await broker.begin(_request(app), "docs")
        sid = started["session_id"]
        recovered = broker.current("docs")
        assert recovered is not None
        assert recovered["session_id"] == sid
        assert recovered["status"] == "waiting"
        assert recovered["authorization_url"] == started["authorization_url"]
        assert broker.current("unrelated") is None
    with _Subject("mallory"):
        assert broker.current("docs") is None
    state = parse_qs(urlsplit(started["authorization_url"]).query)["state"][0]
    assert broker.receive(
        _request(app, urlencode({"state": state, "code": "approved"}))
    )
    with _Subject("alice"):
        recovered = broker.current("docs")
        assert recovered is not None
        assert recovered["status"] == "awaiting_confirmation"
        assert recovered["authorization_url"] == ""
        assert broker.status(sid)["status"] == "awaiting_confirmation"
    assert AgentAuthStore(config.agent_auth_dir).get_tokens("docs") is None
    broker.sessions[sid].expires = 0
    with _Subject("alice"):
        assert broker.current("docs") is None
        with pytest.raises(HTTPException) as exc:
            broker.complete(sid, config.agent_config_dir)
        assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_oauth_rejects_ambiguous_callback_parameters(relay):
    broker, app, _ = relay
    with _Subject("alice"):
        started = await broker.begin(_request(app), "docs")
    sid = started["session_id"]
    state = parse_qs(urlsplit(started["authorization_url"]).query)["state"][0]
    assert not broker.receive(
        _request(
            app,
            urlencode(
                [("state", state), ("state", state), ("code", "approved")]
            ),
        )
    )
    assert not broker.receive(
        _request(
            app,
            urlencode(
                [("state", state), ("code", "approved"), ("code", "other")]
            ),
        )
    )
    with _Subject("alice"):
        assert broker.status(sid)["status"] == "waiting"
    assert broker.receive(
        _request(app, urlencode({"state": state, "error": "access_denied"}))
    )
    broker.cancel(sid)


@pytest.mark.asyncio
async def test_callback_does_not_exchange_until_owner_confirms(relay):
    broker, app, config = relay
    with _Subject("alice"):
        started = await broker.begin(_request(app), "docs")
    sid = started["session_id"]
    state = parse_qs(urlsplit(started["authorization_url"]).query)["state"][0]
    assert broker.receive(
        _request(app, urlencode({"state": state, "code": "approved"}))
    )
    await asyncio.sleep(0.02)
    assert AgentAuthStore(config.agent_auth_dir).get_tokens("docs") is None
    broker.cancel(sid)


@pytest.mark.asyncio
async def test_oauth_denial_cancel_and_expiry(relay):
    broker, app, config = relay
    with _Subject("alice"):
        started = await broker.begin(_request(app), "docs")
    state = parse_qs(urlsplit(started["authorization_url"]).query)["state"][0]
    assert broker.receive(
        _request(app, urlencode({"state": state, "error": "access_denied"}))
    )
    with _Subject("alice"):
        assert (
            broker.status(started["session_id"])["status"]
            == "awaiting_confirmation"
        )
        assert (
            broker.complete(started["session_id"], config.agent_config_dir)[
                "status"
            ]
            == "completing"
        )
    await asyncio.sleep(0.02)
    with _Subject("alice"):
        assert broker.status(started["session_id"])["status"] == "denied"
    with _Subject("alice"):
        again = await broker.begin(_request(app), "docs")
        broker.cancel(again["session_id"])
        with pytest.raises(HTTPException):
            broker.status(again["session_id"])
    with _Subject("alice"):
        expired = await broker.begin(_request(app), "docs")
    broker.sessions[expired["session_id"]].expires = 0
    assert not broker.receive(_request(app, "state=invalid&code=approved"))
    with _Subject("alice"), pytest.raises(HTTPException):
        broker.status(expired["session_id"])


@pytest.mark.asyncio
async def test_rejects_modified_integration_before_token_exchange(
    relay, monkeypatch
):
    broker, app, config = relay
    with _Subject("alice"):
        started = await broker.begin(_request(app), "docs")
    sid = started["session_id"]
    state = parse_qs(urlsplit(started["authorization_url"]).query)["state"][0]
    assert broker.receive(
        _request(app, urlencode({"state": state, "code": "approved"}))
    )
    altered = AgentMcpServerConfig.model_validate(
        {
            "type": "http",
            "url": "https://other.test/mcp",
            "enabled": True,
            "auth": {"mode": "oauth", "scopes": ["tools.read"]},
        }
    )
    monkeypatch.setattr(
        ui,
        "load_agent_manifest",
        lambda *_args, **_kwargs: SimpleNamespace(
            data=SimpleNamespace(mcp_servers={"docs": altered})
        ),
    )
    with _Subject("alice"), pytest.raises(HTTPException) as exc:
        broker.complete(sid, config.agent_config_dir)
    assert exc.value.status_code == 409
    assert AgentAuthStore(config.agent_auth_dir).get_tokens("docs") is None
    with _Subject("alice"), pytest.raises(HTTPException):
        broker.status(sid)


@pytest.mark.asyncio
async def test_clear_or_logout_invalidates_pending_handshake(relay):
    broker, app, config = relay
    with _Subject("alice"):
        started = await broker.begin(_request(app), "docs")
    sid = started["session_id"]
    state = parse_qs(urlsplit(started["authorization_url"]).query)["state"][0]
    assert broker.receive(
        _request(app, urlencode({"state": state, "code": "approved"}))
    )
    with _Subject("alice"):
        assert broker.status(sid)["status"] == "awaiting_confirmation"
    broker.cancel_credential("docs")
    assert not broker.receive(
        _request(app, urlencode({"state": state, "code": "approved"}))
    )
    with _Subject("alice"), pytest.raises(HTTPException) as exc:
        broker.complete(sid, config.agent_config_dir)
    assert exc.value.status_code == 404
    assert AgentAuthStore(config.agent_auth_dir).get_tokens("docs") is None


@pytest.mark.asyncio
async def test_oauth_rejects_incompatible_cli_registration(relay):
    broker, app, config = relay
    store = AgentAuthStore(config.agent_auth_dir)
    store.set_client_info(
        "docs",
        OAuthClientInformationFull.model_validate(
            {
                "client_id": "original-cli-client",
                "redirect_uris": ["http://127.0.0.1:12345/callback"],
            }
        ),
    )
    with (
        _Subject("alice"),
        pytest.raises(HTTPException, match="different callback") as exc,
    ):
        await broker.begin(_request(app), "docs")
    assert exc.value.status_code == 409
    client = store.get_client_info("docs")
    assert client is not None and client.client_id == "original-cli-client"
    assert broker.sessions == {}


@pytest.mark.asyncio
async def test_unsupported_owner_or_missing_scope(relay):
    broker, app, _ = relay
    with _Subject("alice"), pytest.raises(HTTPException):
        await broker.begin(_request(app), "missing")
    token = bind_oauth_claims({"sub": "alice", "scope": "shell:read"})
    try:
        with pytest.raises(Exception, match="executor:use"):
            await broker.begin(_request(app), "docs")
    finally:
        reset_oauth_claims(token)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response_issuer", "granted_scope", "expected"),
    [
        (None, "tools.read", "authorized"),
        ("https://unexpected.test", "tools.read", "failed"),
        (None, "other.scope", "failed"),
    ],
)
async def test_actual_mcp_sdk_pkce_exchange_uses_public_callback(
    relay, monkeypatch, response_issuer, granted_scope, expected
):
    """Exercise the installed MCP SDK's discovery, registration, state and tokens."""
    import json

    import httpx2 as httpx

    from tests.agent.test_agent_oauth_flow import (
        _oauth_metadata,
        _protected_resource,
    )
    from workgate.agent_bridge.auth import build_stored_oauth_provider

    broker, app, config = relay
    monkeypatch.setattr(
        ui, "build_stored_oauth_provider", build_stored_oauth_provider
    )

    def handler(req):
        url = str(req.url)
        if url == "https://mcp.test/mcp":
            if req.headers.get("Authorization") == "Bearer sdk-access":
                return httpx.Response(200, json={"jsonrpc": "2.0"})
            return httpx.Response(
                401,
                headers={
                    "WWW-Authenticate": 'Bearer resource_metadata="https://mcp.test/.well-known/oauth-protected-resource" scope="tools.read"'
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
        if url == "https://auth.test/register":
            registered = json.loads(req.content)
            assert registered["redirect_uris"] == [
                "https://control.test" + ui._CALLBACK
            ]
            return httpx.Response(
                201,
                json={
                    "client_id": "browser-client",
                    "redirect_uris": registered["redirect_uris"],
                    "token_endpoint_auth_method": "none",
                    "grant_types": ["authorization_code", "refresh_token"],
                    "response_types": ["code"],
                },
            )
        if url == "https://auth.test/token":
            form = parse_qs(req.content.decode())
            assert form["code"] == ["approved"]
            assert form["code_verifier"][0]
            return httpx.Response(
                200,
                json={
                    "access_token": "sdk-access",
                    "refresh_token": "sdk-refresh",
                    "token_type": "Bearer",
                    "expires_in": 3600,
                    "scope": granted_scope,
                },
            )
        return httpx.Response(404)

    class RealManager:
        def __init__(
            self, _timeout, _store, *, oauth_provider_factory, allow_stdio
        ):
            self.provider = oauth_provider_factory("docs", None)

        async def list_tools(self, _name, _server):
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(handler), auth=self.provider
            ) as client:
                response = await client.get("https://mcp.test/mcp")
                assert response.status_code == 200
            return []

    monkeypatch.setattr(ui, "AgentMcpClientManager", RealManager)
    with _Subject("alice"):
        started = await broker.begin(_request(app), "docs")
    query = parse_qs(urlsplit(started["authorization_url"]).query)
    assert query["code_challenge_method"] == ["S256"]
    assert query["code_challenge"][0]
    assert query["scope"] == ["tools.read"]
    callback_params = {"state": query["state"][0], "code": "approved"}
    if response_issuer:
        callback_params["iss"] = response_issuer
    assert broker.receive(_request(app, urlencode(callback_params)))
    with _Subject("alice"):
        assert (
            broker.status(started["session_id"])["status"]
            == "awaiting_confirmation"
        )
        assert (
            broker.complete(started["session_id"], config.agent_config_dir)[
                "status"
            ]
            == "completing"
        )
    observed = "completing"
    for _ in range(30):
        await asyncio.sleep(0.02)
        with _Subject("alice"):
            observed = broker.status(started["session_id"])["status"]
        if observed in {"authorized", "failed"}:
            break
    assert observed == expected
    store = AgentAuthStore(config.agent_auth_dir)
    tokens = store.get_tokens("docs")
    client = store.get_client_info("docs")
    if expected == "authorized":
        assert tokens is not None and tokens.access_token == "sdk-access"
    else:
        assert tokens is None
    assert client is not None and client.client_id == "browser-client"
    broker.cancel(started["session_id"])


def test_browser_oauth_callback_is_a_routed_public_endpoint(tmp_path):
    """The callback must be registered as a real route AND OAuth-exempt."""
    from fastapi.testclient import TestClient

    from workgate.config.settings import Settings
    from workgate.control.http.app import build_http_app
    from workgate.control.http.shared_routes import control_http_routes
    from workgate.control.runtime import build_control_runtime

    runtime = build_control_runtime(
        Settings(
            mode="http",
            state_dir=tmp_path / "control",
            base_url="https://control.test",
            auth_mode="oauth",
            oauth_admin_pin="testing-371-only",
        )
    )
    app = build_http_app(runtime=runtime)
    actual = [getattr(route, "path", "") for route in app.routes]
    assert actual.count(ui._CALLBACK) == 1

    _, _, public = control_http_routes(runtime.config, runtime)
    assert ui._CALLBACK in [getattr(route, "path", "") for route in public]
    with TestClient(app, base_url="https://control.test") as client:
        callback = client.get(ui._CALLBACK + "?state=bogus")
        assert callback.status_code == 400  # Not a 404 or auth redirect.
        assert "expired or was already used" in callback.text
        protected = client.post("/api/ui/integrations/oauth/complete", json={})
        assert protected.status_code == 401
