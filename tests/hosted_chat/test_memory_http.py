"""Manual memory API preserves partition identity, quotas, and content policy."""

import httpx
import pytest

from tests.support.hosted_chat_http import provider, runtime_for
from workgate.control.execution_context import control_execution_context
from workgate.control.http.app import build_http_app
from workgate.hosted_chat.agents import AgentRepository
from workgate.hosted_chat.storage import WorkspaceRepository
from workgate.oauth.core.urls import resource_url
from workgate.oauth.protocol.token_codec import issue_access_token

PREFIX = "/api/ui/chat/memories"


@pytest.mark.asyncio
async def test_memory_http_owner_partition_and_optout(tmp_path):
    runtime = runtime_for(tmp_path, auth_mode="oauth")
    db = provider(runtime)
    repo = WorkspaceRepository(db)
    agents = AgentRepository(db)
    aid = agents.create(
        "alice", {"provider": "test-provider", "model": "test-model"}
    )["id"]
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

            def headers(owner):
                return {
                    "Authorization": "Bearer "
                    + issue_access_token(
                        client_id="memory-test",
                        scope="shell:read",
                        resource=resource_url(),
                        subject=owner,
                    )
                }

            alice, bob = headers("alice"), headers("bob")
        assert (await client.get(PREFIX)).status_code == 401
        body = {"key": "style", "value": "Concise responses."}
        assert (
            await client.post(PREFIX, json=body, headers=alice)
        ).status_code == 201
        response = await client.post(
            PREFIX, json={**body, "agentId": aid}, headers=alice
        )
        assert response.status_code == 201
        entry = response.json()["memory"]
        path = PREFIX + "/id/" + entry["_id"]
        assert (await client.get(PREFIX, headers=bob)).json()["memories"] == []
        assert (
            await client.post(
                PREFIX, json={**body, "agentId": aid}, headers=bob
            )
        ).status_code == 404
        assert (
            await client.patch(
                path,
                params={"agentId": aid},
                json={"value": "Other."},
                headers=bob,
            )
        ).status_code == 404
        assert (
            await client.patch(
                path, json={"value": "Wrong pool."}, headers=alice
            )
        ).status_code == 404
        assert (
            await client.post(PREFIX, json=body, headers=alice)
        ).status_code == 409
        assert (
            await client.patch(
                path,
                params={"agentId": aid},
                json={"value": "Detailed."},
                headers=alice,
            )
        ).json()["memory"]["key"] == "style"
        assert (
            await client.patch(
                PREFIX + "/style",
                json={"key": "renamed", "value": "New."},
                headers=alice,
            )
        ).json()["memory"]["key"] == "renamed"
        assert (
            await client.patch(
                PREFIX + "/preferences", json={"memories": False}, headers=alice
            )
        ).json()["memories"] is False
        assert (
            len((await client.get(PREFIX, headers=alice)).json()["memories"])
            == 2
        )
        assert (await client.get(PREFIX + "/preferences", headers=bob)).json()[
            "memories"
        ] is True
        repo.save_setting("alice", "webui.memory.config", {"disabled": True})
        assert (await client.get(PREFIX, headers=alice)).status_code == 403
        agents.delete("alice", aid)
        assert (
            await client.delete(path, params={"agentId": aid}, headers=alice)
        ).status_code == 200
        assert (
            await client.delete(PREFIX + "/renamed", headers=alice)
        ).status_code == 200


@pytest.mark.asyncio
async def test_memory_http_filter_redaction_and_rename(tmp_path):
    runtime = runtime_for(tmp_path)
    db = provider(runtime)
    repo = WorkspaceRepository(db)
    async with (
        runtime.lifespan(),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=build_http_app(runtime=runtime)),
            base_url="http://testserver",
        ) as client,
    ):
        created = await client.post(
            PREFIX, json={"key": "token", "value": "Bearer private-token"}
        )
        assert created.status_code == 201
        path = PREFIX + "/id/" + created.json()["memory"]["_id"]
        repo.save_setting(
            "local-user",
            "webui.filters.config",
            {
                "memories": {
                    "pii": {
                        "action": "block",
                        "starterPatterns": ["bearer_header"],
                    }
                }
            },
        )
        listing = await client.get(PREFIX)
        assert "private-token" not in listing.text
        assert listing.json()["memories"][0]["contentFilterBlocked"]
        denied = await client.patch(path, json={"value": "Bearer new-private"})
        assert denied.status_code == 400
        assert denied.json()["error"] == "content_filter_block"
        assert "new-private" not in denied.text
        assert (
            await client.patch(
                path, json={"key": "cleaned", "value": "Safe text."}
            )
        ).status_code == 200
        assert (await client.get(PREFIX)).json()["memories"][0][
            "key"
        ] == "cleaned"
