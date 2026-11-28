"""Agent authoring transport: owner isolation, revision and policy checks."""

import httpx
import pytest

from tests.support.hosted_chat_http import provider, runtime_for
from workgate.control.execution_context import control_execution_context
from workgate.control.http.app import build_http_app
from workgate.hosted_chat.generation_context import GenerationContextResolver
from workgate.hosted_chat.generations import GenerationStore
from workgate.hosted_chat.storage import WorkspaceRepository
from workgate.oauth.core.urls import resource_url
from workgate.oauth.protocol.token_codec import issue_access_token

PREFIX = "/api/ui/chat/agents"


@pytest.mark.asyncio
async def test_agent_http_authoring_and_revision_fence(tmp_path):
    runtime = runtime_for(tmp_path, auth_mode="oauth")
    db = provider(runtime)
    app = build_http_app(runtime=runtime)
    async with (
        runtime.lifespan(),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
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
                        client_id="agent-test",
                        scope="shell:read",
                        resource=resource_url(),
                        subject=owner,
                    )
                }

            alice, bob = headers("alice"), headers("bob")
        payload = {
            "name": "Research Agent",
            "provider": "test-provider",
            "model": "test-model",
            "instructions": "Original",
            "category": "research",
            "api_key": "never-store",
        }
        assert (await client.get(PREFIX)).status_code == 401
        created = await client.post(PREFIX, json=payload, headers=alice)
        assert created.status_code == 201, created.text
        data = created.json()
        assert data["version"] == 1
        assert "never-store" not in created.text
        path = PREFIX + "/" + data["id"]
        assert (await client.get(PREFIX, headers=bob)).json()["data"] == []
        assert (await client.get(path, headers=bob)).status_code == 404
        assert (
            await client.patch(path, json={"name": "forged"}, headers=bob)
        ).status_code == 404
        assert (await client.delete(path, headers=bob)).status_code == 404
        assert (
            await client.get(path + "/versions", headers=bob)
        ).status_code == 404
        assert (
            await client.post(path + "/duplicate", headers=bob)
        ).status_code == 404
        assert (
            await client.post(
                path + "/revert", json={"version_index": 0}, headers=bob
            )
        ).status_code == 404
        assert (
            await client.get(
                PREFIX,
                params={"category": "research", "search": "Research"},
                headers=alice,
            )
        ).json()["data"][0]["id"] == data["id"]
        generation, _ = GenerationStore(db).begin(
            "alice", {"agent_id": data["id"], "text": "question"}
        )
        resolver = GenerationContextResolver(db, "alice")
        assert resolver.root_agent(generation) == data
        updated = await client.patch(
            path, json={"instructions": "Revised"}, headers=alice
        )
        assert updated.json()["version"] == 2
        with pytest.raises(PermissionError, match="revision changed"):
            resolver.root_agent(generation)
        assert (
            await client.post(
                path + "/revert", json={"version_index": True}, headers=alice
            )
        ).status_code == 400
        reverted = await client.post(
            path + "/revert", json={"version_index": 1}, headers=alice
        )
        assert reverted.json()["instructions"] == "Original"
        assert reverted.json()["version"] == 3
        versions = (await client.get(path + "/versions", headers=alice)).json()
        assert [item["version"] for item in versions] == [3, 2, 1]
        cloned = await client.post(path + "/duplicate", headers=alice)
        assert cloned.status_code == 201
        assert cloned.json()["agent"]["id"] != data["id"]
        assert (await client.delete(path, headers=alice)).status_code == 200
        with pytest.raises(PermissionError, match="no longer owned"):
            resolver.root_agent(generation)


@pytest.mark.asyncio
async def test_agent_http_invalid_configuration_and_policy(tmp_path):
    runtime = runtime_for(tmp_path)
    db = provider(runtime)
    WorkspaceRepository(db).save_setting(
        "local-user",
        "webui.filters.config",
        {
            "agentInstructions": {
                "pii": {"action": "block", "starterPatterns": ["bearer_header"]}
            },
        },
    )
    app = build_http_app(runtime=runtime)
    async with (
        runtime.lifespan(),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client,
    ):
        assert (await client.post(PREFIX, json=[])).status_code == 400
        assert (
            await client.post(PREFIX, json={"name": "missing"})
        ).status_code == 400
        payload = {
            "name": "Agent",
            "provider": "test-provider",
            "model": "test-model",
        }
        blocked = await client.post(
            PREFIX, json={**payload, "instructions": "Bearer private-token"}
        )
        assert blocked.status_code == 400
        assert blocked.json()["error"] == "content_filter_block"
        assert "private-token" not in blocked.text
        created = await client.post(PREFIX, json=payload)
        assert created.status_code == 201
        path = PREFIX + "/" + created.json()["id"]
        assert (
            await client.patch(path, json={"graphAgentIds": ["missing"]})
        ).status_code == 400
        assert (await client.get(path)).json()["version"] == 1
        assert (
            await client.get(PREFIX, params={"limit": "0"})
        ).status_code == 400
