"""Account protocol contracts adapted from pinned test_webui_model_oauth.py.

Source: v6.0.3 blob afb8ef64aac29ac0674444378c111686f042c134.
No live identity provider requests are made.
"""

import asyncio
import base64
import hashlib
import json
import time
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

from workgate.hosted_chat import model_oauth


def token_result(**changes):
    return {
        "scope": model_oauth.SCOPE,
        "expires_in": 3600,
        "token_type": "Bearer",
        "access_token": "mock-access",
        "refresh_token": "mock-refresh",
        "id_token": "mock-id",
        **changes,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,payload,error",
    [
        (200, token_result(), None),
        (200, [], ValueError),
        (400, {"error": "invalid_grant"}, model_oauth.ReauthorizationRequired),
        (
            401,
            {"error": "refresh_token_reused"},
            model_oauth.ReauthorizationRequired,
        ),
        (400, [], ValueError),
        (503, {"error": "unavailable"}, ValueError),
    ],
)
async def test_token_exchange_http_contract(
    monkeypatch, status, payload, error
):
    client_type = httpx.AsyncClient

    def respond(request):
        assert str(request.url) == model_oauth.TOKEN_URL
        assert request.method == "POST"
        assert parse_qs(request.content.decode()) == {
            "grant_type": ["refresh_token"]
        }
        return httpx.Response(status, json=payload)

    monkeypatch.setattr(
        model_oauth,
        "provider_client",
        lambda base, **kwargs: client_type(
            transport=httpx.MockTransport(respond), **kwargs
        ),
    )
    if error:
        with pytest.raises(error):
            await model_oauth._token({"grant_type": "refresh_token"})
    else:
        assert (
            await model_oauth._token({"grant_type": "refresh_token"}) == payload
        )


@pytest.mark.parametrize(
    "changes,error",
    [
        ({"scope": "openid"}, model_oauth.ReauthorizationRequired),
        ({"scope": None}, model_oauth.ReauthorizationRequired),
        ({"expires_in": True}, ValueError),
        ({"expires_in": 0}, ValueError),
        ({"expires_in": 86401}, ValueError),
        ({"token_type": "Basic"}, ValueError),
        ({"access_token": ""}, ValueError),
        ({"refresh_token": "has whitespace"}, ValueError),
        ({"refresh_token": None}, ValueError),
    ],
)
def test_credentials_reject_invalid_response(changes, error):
    with pytest.raises(error):
        model_oauth._credentials(token_result(**changes), {})


def test_credentials_preserve_scope_and_normalize_refresh_time():
    payload = token_result(earliest_refresh_at="invalid")
    payload.pop("scope")
    payload.pop("id_token")
    previous = {"scope": model_oauth.SCOPE, "id_token": "retained"}
    value = model_oauth._credentials(payload, previous)
    assert value["scope"] == model_oauth.SCOPE
    assert value["id_token"] == "retained"
    assert value["earliest_refresh_at"] == 0


@pytest.mark.asyncio
async def test_adapter_exchange_and_refresh_validate_registered_identity(
    monkeypatch,
):
    auth = model_oauth.adapter("chatgpt")
    calls = []

    async def token(data):
        calls.append(data)
        return token_result()

    monkeypatch.setattr(model_oauth, "_token", token)

    async def verify(*args):
        return {"sub": "account"}

    monkeypatch.setattr(model_oauth, "_verify_id", verify)
    query = {"client_id": "oaiapp_mock", "code": "mock-code"}
    for invalid in (
        {"error": "access_denied"},
        {"client_id": "other", "code": "x"},
        {"client_id": "oaiapp_mock"},
    ):
        with pytest.raises(ValueError):
            await auth.exchange(invalid, "redirect", "nonce", "verifier", {})
    assert not calls
    credential = await auth.exchange(query, "redirect", "nonce", "verifier", {})
    assert credential["subject"] == "account"
    assert calls[-1] == {
        "grant_type": "authorization_code",
        "client_id": "oaiapp_mock",
        "code": "mock-code",
        "code_verifier": "verifier",
        "redirect_uri": "redirect",
        "resource": model_oauth.RESOURCE,
    }
    assert (await auth.refresh(credential))["subject"] == "account"
    assert calls[-1]["refresh_token"] == credential["refresh_token"]
    with pytest.raises(ValueError, match="registration"):
        await auth.exchange(
            {**query, "client_id": "oaiapp_other"},
            "redirect",
            "nonce",
            "verifier",
            credential,
        )

    async def other(*args):
        return {"sub": "other-account"}

    monkeypatch.setattr(model_oauth, "_verify_id", other)
    with pytest.raises(ValueError, match="original ChatGPT account"):
        await auth.exchange(query, "redirect", "nonce", "verifier", credential)
    with pytest.raises(
        model_oauth.ReauthorizationRequired, match="identity changed"
    ):
        await auth.refresh(credential)


@pytest.fixture(scope="module")
def identity_key():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(RSAAlgorithm.to_jwk(key.public_key()))
    jwk.update(kid="test-key", alg="RS256", use="sig")
    return key, jwk


@pytest.mark.parametrize(
    "changes,nonce,valid",
    [
        ({}, "nonce", True),
        ({"sub": ""}, "nonce", False),
        ({"nonce": "wrong"}, "nonce", False),
        ({"aud": ["oaiapp_mock", "another"]}, "nonce", False),
        (
            {"aud": ["oaiapp_mock", "another"], "azp": "oaiapp_mock"},
            "nonce",
            True,
        ),
        ({"iss": "https://wrong.invalid"}, "nonce", False),
        ({"aud": "wrong"}, "nonce", False),
        ({"exp": 1}, "nonce", False),
    ],
)
async def test_identity_verified_with_real_signature(
    identity_key, monkeypatch, changes, nonce, valid
):
    key, jwk = identity_key
    calls = []
    original = httpx.AsyncClient

    def handle(request):
        calls.append(request)
        assert str(request.url) == model_oauth.JWKS_URL
        assert "authorization" not in request.headers
        return httpx.Response(200, json={"keys": [jwk]})

    monkeypatch.setattr(
        model_oauth,
        "provider_client",
        lambda base, **kwargs: original(
            transport=httpx.MockTransport(handle), **kwargs
        ),
    )
    claims = {
        "sub": "account",
        "aud": "oaiapp_mock",
        "nonce": "nonce",
        "iss": model_oauth.ISSUER,
        "exp": int(time.time()) + 300,
        **changes,
    }
    token = jwt.encode(
        claims, key, algorithm="RS256", headers={"kid": "test-key"}
    )
    if valid:
        assert (
            await model_oauth._verify_id(token, "oaiapp_mock", nonce) == claims
        )
    else:
        with pytest.raises(ValueError, match="verify ChatGPT identity"):
            await model_oauth._verify_id(token, "oaiapp_mock", nonce)
    assert len(calls) == 1


@pytest.mark.parametrize(
    "kind", ["duplicate", "unknown", "bad-signature", "redirect", "non-rsa"]
)
async def test_identity_rejects_ambiguous_or_untrusted_keys(
    identity_key, monkeypatch, kind
):
    key, jwk = identity_key
    original = httpx.AsyncClient
    keys = [jwk]
    status = 200
    if kind == "duplicate":
        keys = [jwk, jwk]
    elif kind == "unknown":
        keys = [{**jwk, "kid": "other"}]
    elif kind == "bad-signature":
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    elif kind == "redirect":
        status = 302
    else:
        keys = [{**jwk, "kty": "oct"}]
    monkeypatch.setattr(
        model_oauth,
        "provider_client",
        lambda base, **kwargs: original(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    status,
                    json={"keys": keys},
                    headers={"location": "https://wrong.invalid/keys"},
                )
            ),
            **kwargs,
        ),
    )
    token = jwt.encode(
        {
            "sub": "account",
            "aud": "oaiapp_mock",
            "iss": model_oauth.ISSUER,
            "exp": int(time.time()) + 300,
        },
        key,
        algorithm="RS256",
        headers={"kid": "test-key"},
    )
    with pytest.raises(ValueError, match="verify ChatGPT identity"):
        await model_oauth._verify_id(token, "oaiapp_mock", None)


def test_authorization_url_binds_pkce_and_registration():
    auth = model_oauth.adapter("chatgpt")
    redirect = "http://127.0.0.1:54321/auth/callback"
    query = parse_qs(
        urlsplit(
            auth.authorize(
                redirect, "urn:uuid:test", "state", "nonce", "verifier", {}
            )
        ).query
    )
    assert query["client_id"] == ["dynamic_agent_client"]
    assert query["state"] == ["state"] and query["nonce"] == ["nonce"]
    assert query["redirect_uri"] == [redirect]
    assert query["code_challenge"] == [
        base64.urlsafe_b64encode(hashlib.sha256(b"verifier").digest())
        .decode()
        .rstrip("=")
    ]
    assert query["code_challenge_method"] == ["S256"]
    query = parse_qs(
        urlsplit(
            auth.authorize(
                redirect,
                "host",
                "state",
                "nonce",
                "verifier",
                {"client_id": "oaiapp_previous", "id_token": "secret-id"},
            )
        ).query
    )
    assert query["client_id"] == ["oaiapp_previous"]
    assert "id_token" not in query and "secret-id" not in str(query)
    with pytest.raises(ValueError, match="Unsupported"):
        model_oauth.adapter("other")


@pytest.mark.parametrize(
    "suffix",
    [
        "?state=wrong&code=x",
        "?state=state&state=state&code=x",
        "?state=state",
        "?state=state&code=x&error=denied",
        "?state=state&code=x&code=y",
        "?state=state&code=x#fragment",
    ],
)
def test_callback_rejects_invalid_proposals(suffix):
    redirect = "http://127.0.0.1:12345/auth/callback"
    with pytest.raises(ValueError):
        model_oauth.callback_query(redirect + suffix, redirect, "state")
    with pytest.raises(ValueError):
        model_oauth.callback_query(
            "http://127.0.0.1:12346/auth/callback?state=state&code=x",
            redirect,
            "state",
        )


@pytest.fixture
def owned_listeners(monkeypatch):
    servers = []
    start = asyncio.start_server

    async def track(*args, **kwargs):
        server = await start(*args, **kwargs)
        servers.append(server)
        return server

    monkeypatch.setattr(model_oauth.asyncio, "start_server", track)
    return servers


@pytest.mark.parametrize("manual", [False, True])
async def test_real_loopback_and_remote_manual_callback(
    monkeypatch, manual, owned_listeners
):
    async def exchange(self, query, redirect, nonce, verifier, previous):
        assert query["code"] == "mock-code"
        assert query["client_id"] == "oaiapp_mock"
        assert redirect.startswith("http://127.0.0.1:")
        assert nonce and verifier and not previous
        return {"subject": "account"}

    monkeypatch.setattr(model_oauth.ChatGPTAuthorization, "exchange", exchange)
    url, complete, submit, close = await model_oauth.loopback(
        model_oauth.adapter("chatgpt"), "host", {}
    )
    params = parse_qs(urlsplit(url).query)
    redirect = params["redirect_uri"][0]
    target = (
        redirect
        + "?"
        + urlencode(
            {
                "state": params["state"][0],
                "code": "mock-code",
                "client_id": "oaiapp_mock",
            }
        )
    )
    task = asyncio.create_task(complete())
    try:
        if manual:
            with pytest.raises(ValueError):
                submit(redirect + "?state=wrong&code=x")
            submit(target)
        else:
            parts = urlsplit(target)
            reader, writer = await asyncio.open_connection(
                "127.0.0.1", parts.port
            )
            writer.write(
                f"GET {parts.path}?{parts.query} HTTP/1.1\r\nHost: {parts.netloc}\r\n\r\n".encode()
            )
            await writer.drain()
            response = await asyncio.wait_for(reader.read(), 5)
            writer.close()
            await writer.wait_closed()
            assert response.startswith(b"HTTP/1.1 200 OK")
            assert b"mock-code" not in response
        assert await asyncio.wait_for(task, 5) == {"subject": "account"}
        with pytest.raises(ValueError, match="already"):
            submit(target)
        assert len(owned_listeners) == 1
        assert not owned_listeners[0].is_serving()
        assert not owned_listeners[0].sockets
    finally:
        close()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_loopback_cancel_releases_listener(owned_listeners):
    _, complete, _, close = await model_oauth.loopback(
        model_oauth.adapter("chatgpt"), "host", {}
    )
    task = asyncio.create_task(complete())
    close()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(owned_listeners) == 1
    assert not owned_listeners[0].is_serving()
    assert not owned_listeners[0].sockets


async def test_catalog_uses_account_capability(monkeypatch):
    async def fetch(protocol, base, token, **kwargs):
        assert (protocol, base, token) == (
            "openai-responses",
            model_oauth.RESOURCE,
            "token",
        )
        assert kwargs == {"allow_empty": True, "account": True}
        return {"models": ["model"], "model_contexts": {}}

    monkeypatch.setattr(model_oauth, "_fetch_models", fetch)
    assert await model_oauth.adapter("chatgpt").catalog("token") == {
        "models": ["model"],
        "model_contexts": {},
    }


async def test_identity_response_bound_and_sanitized_diagnostic(monkeypatch):
    original = httpx.AsyncClient
    monkeypatch.setattr(model_oauth, "_MAX_RESPONSE_BYTES", 64)
    for response in (
        httpx.Response(200, content=b"x" * 65),
        httpx.Response(503, text="private-credential"),
        httpx.Response(302, headers={"location": "https://elsewhere.invalid/"}),
    ):
        monkeypatch.setattr(
            model_oauth,
            "provider_client",
            lambda base, response=response, **kwargs: original(
                transport=httpx.MockTransport(lambda request: response),
                **kwargs,
            ),
        )
        with pytest.raises(
            ValueError, match="ChatGPT token exchange failed"
        ) as exc:
            await model_oauth._token({"refresh_token": "private-credential"})
        assert "private-credential" not in str(exc.value)


async def test_loopback_invalid_http_and_expiry(monkeypatch, owned_listeners):
    monkeypatch.setattr(model_oauth, "TTL", 1)
    url, complete, _, close = await model_oauth.loopback(
        model_oauth.adapter("chatgpt"), "host", {}
    )
    redirect = urlsplit(parse_qs(urlsplit(url).query)["redirect_uri"][0])
    try:
        reader, writer = await asyncio.open_connection(
            "127.0.0.1", redirect.port
        )
        writer.write(b"POST /auth/callback HTTP/1.1\r\nHost: localhost\r\n\r\n")
        await writer.drain()
        assert (await asyncio.wait_for(reader.read(), 5)).startswith(
            b"HTTP/1.1 400 Bad Request"
        )
        writer.close()
        await writer.wait_closed()
        with pytest.raises(TimeoutError):
            await complete()
        assert not owned_listeners[0].is_serving()
        assert not owned_listeners[0].sockets
    finally:
        close()
