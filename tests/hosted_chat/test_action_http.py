"""Native Action routes are bounded, authenticated and never reveal credentials."""

import json

import httpx
import pytest

from tests.support.hosted_chat_http import runtime_for
from workgate.control.execution_context import control_execution_context
from workgate.control.http.app import build_http_app
from workgate.hosted_chat.actions import ActionRepository
from workgate.hosted_chat.agents import AgentRepository
from workgate.hosted_chat.database import SQLiteDatabase
from workgate.oauth.core.urls import resource_url
from workgate.oauth.protocol.token_codec import issue_access_token

PREFIX = "/api/ui/chat/agents/actions"


def body():
    return {
        "metadata": {
            "domain": "https://example.org",
            "raw_spec": json.dumps(
                {
                    "servers": [{"url": "https://example.org"}],
                    "paths": {"/ping": {"get": {"operationId": "ping"}}},
                }
            ),
            "auth": {"type": "service_http", "authorization_type": "bearer"},
            "api_key": "private-action-key",
        },
        "functions": [{"function": {"name": "ping"}}],
    }


@pytest.mark.asyncio
async def test_action_http_crud_and_scopes(tmp_path, monkeypatch):
    runtime = runtime_for(tmp_path, auth_mode="oauth")
    db = SQLiteDatabase(runtime.config.state_dir)
    agents = AgentRepository(db)
    alice_agent = agents.create(
        "alice", {"name": "Alice", "provider": "openAI", "model": None}
    )
    bob_agent = agents.create(
        "bob", {"name": "Bob", "provider": "openAI", "model": None}
    )
    async with (
        runtime.lifespan(),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=build_http_app(runtime=runtime)),
            base_url="http://testserver",
        ) as client,
    ):
        with control_execution_context(
            config=runtime.config,
            state_store=runtime.state_store,
            oauth_state=runtime.oauth_state,
        ):

            def headers(owner, scope):
                return {
                    "Authorization": "Bearer "
                    + issue_access_token(
                        client_id="action-test",
                        scope=scope,
                        resource=resource_url(),
                        subject=owner,
                    )
                }

            alice = headers("alice", "shell:read shell:write")
            bob = headers("bob", "shell:read shell:write")
            read = headers("alice", "shell:read")
            narrow = headers("alice", "audit:read")
        path = PREFIX + "/" + alice_agent["id"]
        assert (await client.get(PREFIX)).status_code == 401
        assert (await client.get(PREFIX, headers=narrow)).status_code == 403
        assert (
            await client.post(path, json=body(), headers=read)
        ).status_code == 403
        assert (
            await client.post(path, json=body(), headers=bob)
        ).status_code == 404
        response = await client.post(path, json=body(), headers=alice)
        assert response.status_code == 200, response.text
        updated, saved = response.json()
        assert "private-action-key" not in response.text
        assert "api_key" not in response.text
        assert response.headers["cache-control"] == "no-store"
        assert updated["id"] == alice_agent["id"]
        assert (await client.get(PREFIX, headers=read)).json() == [saved]
        assert (await client.get(PREFIX, headers=bob)).json() == []
        delete = path + "/" + saved["action_id"]
        assert (await client.delete(delete, headers=read)).status_code == 403
        assert (await client.delete(delete, headers=bob)).status_code == 404
        forged = body()
        forged["action_id"] = saved["action_id"]
        assert (
            await client.post(
                PREFIX + "/" + bob_agent["id"], headers=bob, json=forged
            )
        ).status_code == 403
        assert (await client.delete(delete, headers=alice)).status_code == 200
        assert (await client.delete(delete, headers=alice)).status_code == 404
        assert (await client.get(PREFIX, headers=alice)).json() == []
        assert (
            await client.post(path, headers=alice, content=b"not-json")
        ).status_code == 400
        assert (
            await client.post(path, headers=alice, json=[])
        ).status_code == 400
        from workgate.control.http import hosted_chat_actions

        monkeypatch.setattr(hosted_chat_actions, "_MAX_BODY", 32)
        assert (
            await client.post(path, headers=alice, json=body())
        ).status_code == 400
        assert ActionRepository(db).list("alice") == []


@pytest.mark.asyncio
async def test_action_routes_disabled(tmp_path):
    runtime = runtime_for(tmp_path, enabled=False)
    async with (
        runtime.lifespan(),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=build_http_app(runtime=runtime)),
            base_url="http://testserver",
        ) as client,
    ):
        assert (await client.get(PREFIX)).status_code == 404
        assert not (runtime.config.state_dir / "hosted-chat").exists()


def test_repository_revocation_rolls_back_action_and_agent(tmp_path):
    db = SQLiteDatabase(tmp_path)
    agents = AgentRepository(db)
    repo = ActionRepository(db)
    agent = agents.create(
        "alice", {"name": "Alice", "provider": "openAI", "model": None}
    )
    calls = 0

    def check():
        nonlocal calls
        calls += 1
        if calls == 3:
            raise PermissionError("revoked")

    with pytest.raises(PermissionError):
        repo.save("alice", agent["id"], body(), check_grant=check)
    assert repo.list("alice") == []
    assert agents.get("alice", agent["id"]) == agent
    updated, saved = repo.save("alice", agent["id"], body())
    calls = 1
    with pytest.raises(PermissionError):
        repo.delete("alice", agent["id"], saved["action_id"], check_grant=check)
    assert repo.list("alice") == [saved]
    assert agents.get("alice", agent["id"]) == updated
