"""External ingress uses only its source bearer, never Control OAuth."""

import httpx
import pytest

from tests.support.hosted_chat_http import provider, runtime_for
from workgate.control import hosted_chat_event_host as host
from workgate.control.execution_context import control_execution_context
from workgate.control.http import hosted_chat_events as routes
from workgate.control.http.app import build_http_app
from workgate.hosted_chat.agents import AgentRepository
from workgate.hosted_chat.generation_actor_lease import ActorLease
from workgate.hosted_chat.generations import GenerationStore
from workgate.oauth.core.urls import resource_url
from workgate.oauth.protocol.token_codec import issue_access_token

ADMIN = "/api/ui/chat/event-source-keys"
INGRESS = "/api/agents/v1/events"


@pytest.mark.asyncio
async def test_event_http_end_to_end(tmp_path, monkeypatch):
    runtime = runtime_for(tmp_path, auth_mode="oauth")
    db = provider(runtime)
    agent = AgentRepository(db).create(
        "alice",
        {
            "name": "Research",
            "provider": "test-provider",
            "model": "test-model",
            "subagents": {"enabled": True, "allowSelf": True},
        },
    )
    store = GenerationStore(db)
    parent, _ = store.begin(
        "alice", {"text": "Parent", "agent_id": agent["id"]}
    )
    lease = ActorLease.claim(db, "alice", parent.run_id)
    assert lease is not None
    store.finish(
        "alice",
        parent.run_id,
        status="complete",
        text="Reply",
        actor_lease=lease,
    )
    lease.release()
    original = host.run_event_generation
    seen = []

    async def stream(profile, messages, **kwargs):
        seen.append(messages)
        assert "tools" not in kwargs
        yield "Event answer."

    async def execute(runtime, database, current):
        return await original(runtime, database, current, stream_text=stream)

    monkeypatch.setattr(host, "run_event_generation", execute)
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
                        client_id="event-test",
                        scope=scope,
                        resource=resource_url(),
                        subject=owner,
                    )
                }

            alice = headers("alice", "shell:read shell:write")
            reader = headers("alice", "shell:read")
            bob = headers("bob", "shell:read shell:write")
        assert (await client.get(ADMIN)).status_code == 401
        assert (
            await client.post(ADMIN, headers=reader, json={})
        ).status_code == 403
        assert (
            await client.post(ADMIN, headers=alice, json={"owner": "bob"})
        ).status_code == 400
        minted = await client.post(ADMIN, headers=alice, json={"label": "Test"})
        assert minted.status_code == 201, minted.text
        key = minted.json()
        assert minted.headers["cache-control"] == "no-store"
        assert key["permissions"] == ["continue", "fire"]
        source = {
            "Authorization": "Bearer " + key["token"],
            "Idempotency-Key": "once",
        }
        assert (
            key["token"] not in (await client.get(ADMIN, headers=reader)).text
        )
        assert (await client.get(ADMIN, headers=bob)).json() == {"sources": []}
        assert (await client.get(ADMIN, headers=source)).status_code == 401
        assert (
            await client.delete(ADMIN + "/" + key["id"], headers=bob)
        ).status_code == 404

        body = {
            "mode": "fire",
            "event": {"id": "one", "type": "update", "occurredAt": 123},
            "target": {"agentId": agent["id"]},
            "input": "Review",
        }
        assert (await client.post(INGRESS, json=body)).status_code == 401
        assert (
            await client.post(INGRESS, headers=alice, json=body)
        ).status_code == 401
        assert (
            await client.post(
                INGRESS,
                headers={"Authorization": source["Authorization"]},
                json=body,
            )
        ).status_code == 400
        assert (
            await client.post(
                INGRESS,
                headers=[*source.items(), ("Idempotency-Key", "again")],
                json=body,
            )
        ).status_code == 400
        assert (
            await client.post(
                INGRESS,
                headers=[
                    *source.items(),
                    ("Authorization", source["Authorization"]),
                ],
                json=body,
            )
        ).status_code == 401
        assert (
            await client.post(INGRESS, headers=source, content=b"{}")
        ).status_code == 400
        assert (
            await client.post(
                INGRESS,
                headers={**source, "Content-Type": "application/json"},
                content=b'{"mode":"fire","mode":"continue"}',
            )
        ).status_code == 400
        assert (
            await client.post(
                INGRESS,
                headers={**source, "Content-Type": "application/json"},
                content=b'{"value":NaN}',
            )
        ).status_code == 400
        with monkeypatch.context() as patch:
            patch.setattr(routes, "_MAX_BODY", 4)
            assert (
                await client.post(INGRESS, headers=source, json=body)
            ).status_code == 413

        first = await client.post(INGRESS, headers=source, json=body)
        assert first.status_code == 202, first.text
        url = first.headers["location"]
        assert first.json()["id"] in url
        replay = await client.post(INGRESS, headers=source, json=body)
        assert replay.json()["replayed"] is True
        assert (
            await client.post(
                INGRESS, headers=source, json={**body, "input": "different"}
            )
        ).status_code == 409
        assert (await client.get(url, headers=alice)).status_code == 401
        other = (await client.post(ADMIN, headers=alice, json={})).json()
        assert (
            await client.get(
                url, headers={"Authorization": "Bearer " + other["token"]}
            )
        ).status_code == 404
        binding_body = {
            "actorId": "observer",
            "parentConversationId": parent.conversation_id,
            "parentMessageId": parent.response_message_id,
            "target": {"agentId": agent["id"]},
        }
        binding = await client.post(
            INGRESS + "/bindings",
            headers={**source, "Idempotency-Key": "bind"},
            json=binding_body,
        )
        assert binding.status_code == 201, binding.text
        assert (
            await client.post(INGRESS + "/bindings", headers=source, json={})
        ).status_code == 400
        second = await client.post(
            INGRESS,
            headers={**source, "Idempotency-Key": "continue"},
            json={
                **body,
                "mode": "continue",
                "bindingId": binding.json()["id"],
            },
        )
        assert second.status_code == 202, second.text
        # Wake the existing supervised dispatcher rather than creating an actor
        # from HTTP credentials. Both requests must settle exactly once.
        import asyncio

        receipt = continuation = {}
        for _ in range(100):
            receipt = (await client.get(url, headers=source)).json()
            continuation = (
                await client.get(second.headers["location"], headers=source)
            ).json()
            if receipt["status"] == continuation["status"] == "succeeded":
                break
            await asyncio.sleep(0.05)
        assert receipt["status"] == continuation["status"] == "succeeded"
        assert len(seen) == 2
        run = "/api/ui/chat/generations/" + receipt["result"]["runId"]
        assert (await client.get(run, headers=reader)).status_code == 200
        assert (
            await client.post(run + "/attach", headers=alice)
        ).status_code == 404
        assert (await client.get(run, headers=source)).status_code == 401
        assert (
            await client.delete(ADMIN + "/" + key["id"], headers=reader)
        ).status_code == 403
        assert (
            await client.delete(ADMIN + "/" + key["id"], headers=alice)
        ).status_code == 200
        assert (await client.get(url, headers=source)).status_code == 401


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
async def test_event_ingress_never_uses_noauth_bypass(tmp_path, enabled):
    runtime = runtime_for(tmp_path, enabled=enabled)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=build_http_app(runtime=runtime)),
        base_url="http://testserver",
    ) as client:
        assert (await client.post(INGRESS, json={})).status_code == (
            401 if enabled else 404
        )
