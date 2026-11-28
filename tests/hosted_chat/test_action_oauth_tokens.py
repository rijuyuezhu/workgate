"""Action OAuth token version binding and durable refresh claims."""

import asyncio
import json
import time
from urllib.parse import parse_qs

import httpx
import pytest

from workgate.hosted_chat import action_oauth_tokens as oauth
from workgate.hosted_chat.actions import ActionRepository
from workgate.hosted_chat.agents import AgentRepository
from workgate.hosted_chat.credential_cipher import CredentialUnavailable
from workgate.hosted_chat.database import SQLiteDatabase


def body():
    return {
        "metadata": {
            "domain": "https://api.example.org",
            "raw_spec": json.dumps(
                {
                    "openapi": "3.0.3",
                    "servers": [{"url": "https://api.example.org"}],
                    "paths": {"/ping": {"get": {"operationId": "ping"}}},
                }
            ),
            "auth": {
                "type": "oauth",
                "token_exchange_method": "default_post",
                "scope": "read",
                "authorization_url": "https://auth.example.org/auth",
                "client_url": "https://auth.example.org/token",
            },
            "oauth_client_id": "client",
            "oauth_client_secret": "secret",
        },
        "functions": [{"type": "function", "function": {"name": "ping"}}],
    }


@pytest.fixture
def records(tmp_path):
    db = SQLiteDatabase(tmp_path)
    agents = AgentRepository(db)
    agent = agents.create(
        "alice", {"name": "Agent", "provider": "openAI", "model": None}
    )
    repo = ActionRepository(db)
    _, action = repo.save("alice", agent["id"], body())
    return db, repo, agent, action, oauth.ActionOAuthTokens(repo)


@pytest.mark.asyncio
async def test_owner_revision_and_kind_binding(records):
    db, repo, agent, action, tokens = records
    ident = action["action_id"]
    args = ("alice", ident, 1)
    assert await tokens.access_token(*args) is None
    tokens.store_code_tokens(
        *args,
        {
            "access_token": "access-value",
            "refresh_token": "refresh-value",
            "expires_in": "60",
        },
    )
    assert await tokens.access_token(*args) == "access-value"
    assert b"access-value" not in db.path.read_bytes()
    assert "access-value" not in json.dumps(repo.list("alice"))
    with pytest.raises(PermissionError):
        await tokens.access_token("bob", ident, 1)
    with pytest.raises(PermissionError):
        await tokens.access_token("alice", ident, 2)
    with db.connect() as conn:
        conn.execute(
            "UPDATE action_oauth_tokens SET token=(SELECT token FROM action_oauth_tokens WHERE kind='refresh') WHERE kind='access'"
        )
    with pytest.raises(CredentialUnavailable):
        await tokens.access_token(*args)
    patch = body()
    patch["action_id"] = ident
    repo.save("alice", agent["id"], patch)
    assert await tokens.access_token("alice", ident, 2) is None
    with pytest.raises(PermissionError):
        tokens.store_code_tokens(*args, {"access_token": "late"})


@pytest.mark.asyncio
async def test_refresh_serialized_and_preserved(records, monkeypatch):
    _, _, _, action, tokens = records
    args = ("alice", action["action_id"], 1)
    tokens.store_code_tokens(
        *args,
        {"access_token": "old", "refresh_token": "rotate", "expires_in": 0},
    )
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def exchange(endpoint, form, credentials, method):
        calls.append(form)
        entered.set()
        await release.wait()
        return {"access_token": "fresh", "expires_in": 60}

    monkeypatch.setattr(oauth, "exchange", exchange)
    first = asyncio.create_task(tokens.access_token(*args))
    await entered.wait()
    second = asyncio.create_task(tokens.access_token(*args))
    await asyncio.sleep(0)
    release.set()
    assert await asyncio.gather(first, second) == ["fresh", "fresh"]
    assert calls == [{"grant_type": "refresh_token", "refresh_token": "rotate"}]
    with tokens.database.connect() as conn:
        conn.execute(
            "UPDATE action_oauth_tokens SET expires_at=0 WHERE kind='access'"
        )
    assert await tokens.access_token(*args) == "fresh"
    assert len(calls) == 2
    tokens.store_code_tokens(*args, {"access_token": "new", "expires_in": 0})
    assert await tokens.access_token(*args) is None
    assert len(calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["cancel", "lost", "expired"])
async def test_uncertain_rotation_never_retries(records, monkeypatch, failure):
    db, _, _, action, tokens = records
    args = ("alice", action["action_id"], 1)
    tokens.store_code_tokens(
        *args,
        {"access_token": "old", "refresh_token": "rotate", "expires_in": 0},
    )
    entered = asyncio.Event()
    calls = []

    async def exchange(*unused):
        calls.append(True)
        entered.set()
        if failure == "lost":
            raise ConnectionError("lost response")
        await asyncio.Event().wait()

    monkeypatch.setattr(oauth, "exchange", exchange)
    if failure == "expired":
        with db.connect() as conn:
            conn.execute(
                "INSERT INTO action_oauth_tokens(action_id,owner,kind,token,expires_at) VALUES(?,?,?,?,?)",
                (
                    args[1],
                    "alice",
                    "refresh_claim",
                    "dead",
                    int(time.time()) - 1,
                ),
            )
    else:
        task = asyncio.create_task(tokens.access_token(*args))
        await entered.wait()
        if failure == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            assert await task is None
    assert await tokens.access_token(*args) is None
    assert len(calls) == (0 if failure == "expired" else 1)
    with db.read_snapshot() as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM action_oauth_tokens").fetchone()[
                0
            ]
            == 0
        )


@pytest.mark.asyncio
async def test_new_code_wins_over_inflight_refresh(records, monkeypatch):
    _, _, _, action, tokens = records
    args = ("alice", action["action_id"], 1)
    tokens.store_code_tokens(
        *args,
        {"access_token": "old", "refresh_token": "rotate", "expires_in": 0},
    )
    entered = asyncio.Event()
    release = asyncio.Event()

    async def exchange(*unused):
        entered.set()
        await release.wait()
        return {"access_token": "late", "refresh_token": "late-refresh"}

    monkeypatch.setattr(oauth, "exchange", exchange)
    task = asyncio.create_task(tokens.access_token(*args))
    await entered.wait()
    tokens.store_code_tokens(*args, {"access_token": "new-code"})
    release.set()
    with pytest.raises(PermissionError):
        await task
    assert await tokens.access_token(*args) == "new-code"


@pytest.mark.parametrize(
    "result",
    [
        None,
        {},
        {"access_token": ""},
        {"access_token": "a", "token_type": "mac"},
        {"access_token": "a\n"},
        {"access_token": "a", "refresh_token": 2},
        {"access_token": "a", "expires_in": True},
        {"access_token": "a", "expires_in": -1},
        {"access_token": "a", "expires_in": "1.2"},
    ],
)
def test_reject_malformed_tokens(result):
    with pytest.raises(ValueError):
        oauth._validated_token(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["default_post", "basic_auth_header"])
async def test_exchange_uses_pinned_transport_and_auth(monkeypatch, method):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(
            200,
            stream=httpx.ByteStream(
                b'{"access_token":"a","expires_in":60,"extra":"ignored"}'
            ),
        )

    def transport(origin):
        assert origin == "https://auth.example.org/token"
        return httpx.MockTransport(handler)

    monkeypatch.setattr(oauth, "ActionHTTPTransport", transport)
    assert await oauth.exchange(
        "https://auth.example.org/token?q=1",
        {"grant_type": "authorization_code", "code": "code"},
        {"oauth_client_id": "client", "oauth_client_secret": "secret"},
        method,
    ) == {"access_token": "a", "expires_in": 60}
    [request] = seen
    assert request.url.query == b"q=1"
    form = parse_qs(request.content.decode())
    assert form["code"] == ["code"]
    if method == "default_post":
        assert form["client_secret"] == ["secret"]
        assert "authorization" not in request.headers
    else:
        assert "client_secret" not in form
        assert request.headers["authorization"] == "Basic Y2xpZW50OnNlY3JldA=="


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,headers,content,exception",
    [
        (302, {}, b"", PermissionError),
        (200, {"content-encoding": "gzip"}, b"", ValueError),
        (200, {}, b"x" * 32769, ValueError),
        (200, {}, b"{", ValueError),
    ],
)
async def test_exchange_rejects_unsafe_responses(
    monkeypatch, status, headers, content, exception
):
    monkeypatch.setattr(
        oauth,
        "ActionHTTPTransport",
        lambda _: httpx.MockTransport(
            lambda request: httpx.Response(
                status, headers=headers, stream=httpx.ByteStream(content)
            )
        ),
    )
    with pytest.raises(exception):
        await oauth.exchange(
            "https://auth.example.org/token",
            {},
            {"oauth_client_id": "c", "oauth_client_secret": "s"},
            "default_post",
        )
