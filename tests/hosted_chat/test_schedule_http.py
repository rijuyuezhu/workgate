"""Owner-authenticated schedules and strictly observational scheduled streams."""

import httpx
import pytest

from tests.support.hosted_chat_http import provider, runtime_for
from workgate.control import hosted_chat_schedule_host as host
from workgate.control.execution_context import control_execution_context
from workgate.control.http.app import build_http_app
from workgate.hosted_chat.agents import AgentRepository
from workgate.oauth.core.urls import resource_url
from workgate.oauth.protocol.token_codec import issue_access_token

PREFIX = "/api/ui/chat"


@pytest.mark.asyncio
async def test_schedule_http_owner_scope_run_and_observation(
    tmp_path, monkeypatch
):
    runtime = runtime_for(tmp_path, auth_mode="oauth")
    db = provider(runtime)
    agent = AgentRepository(db).create(
        "alice",
        {
            "name": "Research",
            "provider": "test-provider",
            "model": "test-model",
        },
    )
    seen = []
    original = host.run_scheduled_generation

    async def stream(profile, messages, **kwargs):
        assert messages[-1]["content"] == "Review"
        assert "tools" not in kwargs
        seen.append(messages)
        yield "Scheduled answer."

    async def execute(runtime, database, occurrence):
        return await original(runtime, database, occurrence, stream_text=stream)

    monkeypatch.setattr(host, "run_scheduled_generation", execute)
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

            def headers(owner, scope):
                return {
                    "Authorization": "Bearer "
                    + issue_access_token(
                        client_id="schedule-test",
                        scope=scope,
                        resource=resource_url(),
                        subject=owner,
                    )
                }

            alice = headers("alice", "shell:read shell:write")
            reader = headers("alice", "shell:read")
            bob = headers("bob", "shell:read shell:write")
            no_read = headers("alice", "shell:execute")
        url = PREFIX + "/schedules"
        payload = {
            "name": "Daily",
            "prompt": "Review",
            "agent_id": agent["id"],
            "timezone": "UTC",
            "cadence": {"frequency": "daily", "hour": 9, "minute": 0},
            "enabled": False,
            "clientRequestId": "request-1",
        }
        assert (await client.get(url, headers=alice)).json()["schedules"] == []
        assert (
            await client.post(url, headers=reader, json=payload)
        ).status_code == 403
        assert (
            await client.post(url, headers=bob, json=payload)
        ).status_code == 404
        assert (
            await client.post(url, headers=alice, json=[])
        ).status_code == 400
        response = await client.post(url, headers=alice, json=payload)
        assert response.status_code == 201, response.text
        sid = response.json()["id"]
        path = url + "/" + sid
        assert (await client.get(path, headers=bob)).status_code == 404
        assert (await client.get(path, headers=reader)).json()["id"] == sid
        assert (await client.get(url, headers=no_read)).status_code == 403
        assert (
            await client.patch(path, headers=reader, json={"name": "No"})
        ).status_code == 403
        assert (
            await client.patch(
                path,
                headers=alice,
                json={"name": "Updated", "expectedConfigRevision": 0},
            )
        ).status_code == 200
        assert (
            await client.patch(
                path,
                headers=alice,
                json={"name": "Conflict", "expectedConfigRevision": 0},
            )
        ).status_code == 409
        assert (
            await client.post(path + "/run", headers=bob)
        ).status_code == 404
        assert (
            await client.post(path + "/run", headers=reader)
        ).status_code == 403
        launched = await client.post(path + "/run", headers=alice)
        assert launched.status_code == 202, launched.text
        item = launched.json()
        assert item["scheduleId"] == sid
        run = PREFIX + "/generations/" + item["runId"]
        assert (
            await client.post(run + "/attach", headers=alice)
        ).status_code == 404
        assert (
            await client.post(run + "/abort", headers=alice)
        ).status_code == 404
        assert (
            await client.get(run + "/action", headers=reader)
        ).status_code == 404
        assert (await client.get(run, headers=reader)).status_code == 200
        assert (await client.get(run, headers=bob)).status_code == 404
        assert (await client.get(run, headers=no_read)).status_code == 403
        events = await client.get(run + "/events", headers=reader)
        assert events.status_code == 200
        assert "Scheduled answer." in events.text
        assert "test-secret" not in events.text
        assert len(seen) == 1
        assert (
            await client.post(path + "/run", headers=alice)
        ).status_code == 409
        assert await runtime.generation_supervisor.wait_idle(
            "alice", item["runId"]
        )
        assert (await client.get(path, headers=reader)).json()["runCount"] == 1
        assert (await client.delete(path, headers=reader)).status_code == 403
        assert (await client.delete(path, headers=alice)).status_code == 200


@pytest.mark.asyncio
async def test_disabled_schedule_routes(tmp_path):
    runtime = runtime_for(tmp_path, enabled=False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=build_http_app(runtime=runtime)),
        base_url="http://testserver",
    ) as client:
        assert (await client.get(PREFIX + "/schedules")).status_code == 404
