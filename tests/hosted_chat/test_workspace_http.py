"""Real authenticated workspace routes; no model or machine effects required."""

import httpx
import pytest

from tests.support.hosted_chat_http import provider, runtime_for
from workgate.control.execution_context import control_execution_context
from workgate.control.http.app import build_http_app
from workgate.hosted_chat.generation_actor_lease import ActorLease
from workgate.hosted_chat.generations import GenerationStore
from workgate.hosted_chat.storage import WorkspaceRepository
from workgate.oauth.core.urls import resource_url
from workgate.oauth.protocol.token_codec import issue_access_token

PREFIX = "/api/ui/chat"


@pytest.mark.asyncio
async def test_workspace_http_owner_isolation_metadata_and_unread(tmp_path):
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
                        client_id="workspace-test",
                        scope="shell:read",
                        resource=resource_url(),
                        subject=owner,
                    )
                }

            alice, bob = headers("alice"), headers("bob")

        projects = PREFIX + "/projects"
        conversations = PREFIX + "/conversations"
        assert (await client.get(projects)).status_code == 401
        p = await client.post(
            projects, json={"name": "Research", "color": "blue"}, headers=alice
        )
        assert p.status_code == 201, p.text
        pid = p.json()["id"]
        assert (
            await client.get(projects + "/" + pid, headers=bob)
        ).status_code == 404
        assert (await client.get(projects, headers=bob)).json()[
            "projects"
        ] == []
        assert (
            await client.post(
                conversations,
                json={"title": "forged", "projectId": pid},
                headers=bob,
            )
        ).status_code == 404
        created = await client.post(
            conversations,
            json={
                "title": "Research notes",
                "projectId": pid,
                "tags": ["experiment"],
                "lsmSampling": {"temperature": 0.5},
            },
            headers=alice,
        )
        assert created.status_code == 201, created.text
        cid = created.json()["conversationId"]
        path = conversations + "/" + cid
        assert (await client.get(path, headers=bob)).status_code == 404
        assert (
            await client.patch(path, json={"title": "other"}, headers=bob)
        ).status_code == 404
        assert (await client.delete(path, headers=bob)).status_code == 404
        assert (
            await client.patch(
                path,
                json={"nativeAdmission": {"scopes": ["forged"]}},
                headers=alice,
            )
        ).status_code == 400
        assert (
            await client.patch(path, json={"pinned": "true"}, headers=alice)
        ).status_code == 400
        assert (
            await client.patch(
                path, json={"lsmSampling": {"temperature": 9}}, headers=alice
            )
        ).status_code == 400
        assert (
            await client.patch(path, json={"pinned": True}, headers=alice)
        ).json()["pinned"]
        assert (
            len(
                (
                    await client.get(
                        conversations,
                        params={
                            "pinned": "true",
                            "projectId": pid,
                            "tag": "experiment",
                        },
                        headers=alice,
                    )
                ).json()["conversations"]
            )
            == 1
        )
        assert (
            await client.get(conversations, params={"limit": 0}, headers=alice)
        ).status_code == 400

        # Generate persisted history without launching a model worker.
        generation, _ = GenerationStore(db).begin(
            "alice",
            {
                "conversationId": cid,
                "text": "secret workspace note",
                "provider": "test-provider",
                "model": "test-model",
            },
            native_admission={"scopes": ["shell:read"], "task_machine": None},
        )
        assert (await client.delete(path, headers=alice)).status_code == 403
        lease = ActorLease.claim(db, "alice", generation.run_id)
        assert lease is not None
        assert GenerationStore(db).finish(
            "alice",
            generation.run_id,
            status="complete",
            text="answer",
            actor_lease=lease,
        )
        result = (
            await client.get(
                path + "/messages", params={"limit": 1}, headers=alice
            )
        ).json()
        assert len(result["messages"]) == 1 and result["nextCursor"]
        assert (
            await client.get(path + "/messages", headers=bob)
        ).status_code == 404
        assert (
            await client.post(
                path + "/messages",
                json={"role": "assistant", "text": "forged"},
                headers=alice,
            )
        ).status_code == 405
        assert (
            len(
                (
                    await client.get(
                        PREFIX + "/messages/search",
                        params={"search": "secret workspace note"},
                        headers=alice,
                    )
                ).json()["messages"]
            )
            == 1
        )
        assert (
            await client.get(
                PREFIX + "/messages/search",
                params={"search": "secret workspace note"},
                headers=bob,
            )
        ).json()["messages"] == []
        marker = (
            await client.get(conversations + "/unread", headers=alice)
        ).json()["unread"][cid]
        assert (
            await client.get(conversations + "/unread", headers=bob)
        ).json()["unread"] == {}
        unread = path + "/unread"
        assert (
            await client.post(
                unread, json={"unread": False, "expected": "old"}, headers=alice
            )
        ).json()["marker"] == marker
        assert (
            await client.post(unread, json={"unread": False}, headers=alice)
        ).json()["marker"] == marker
        assert (
            await client.post(
                unread,
                json={"unread": False, "expected": marker},
                headers=alice,
            )
        ).json()["marker"] is None
        manual = (
            await client.post(unread, json={"unread": True}, headers=alice)
        ).json()["marker"]
        assert manual.startswith("manual:")
        assert (
            await client.post(
                unread,
                json={"unread": False, "expected": marker},
                headers=alice,
            )
        ).json()["marker"] == manual
        assert (
            await client.patch(path, json={"isArchived": True}, headers=alice)
        ).json()["isArchived"]
        assert (await client.get(conversations, headers=alice)).json()[
            "conversations"
        ] == []
        assert (
            len(
                (
                    await client.get(
                        conversations,
                        params={"archived": "true"},
                        headers=alice,
                    )
                ).json()["conversations"]
            )
            == 1
        )
        assert (
            await client.delete(projects + "/" + pid, headers=alice)
        ).status_code == 200
        current = WorkspaceRepository(db).conversation("alice", cid)
        assert current is not None and current["projectId"] is None
        assert (await client.delete(path, headers=alice)).status_code == 200
        assert (await client.get(path, headers=alice)).status_code == 404
