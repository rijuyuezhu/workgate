"""Live native permissions and approval preferences through the owner API."""

import httpx
import pytest

from tests.support.hosted_chat_http import provider, runtime_for
from workgate.control.execution_context import control_execution_context
from workgate.control.http.app import build_http_app
from workgate.control.state import ExecutorTrustRecord
from workgate.hosted_chat.agents import AgentRepository
from workgate.hosted_chat.storage import WorkspaceRepository
from workgate.oauth.core.urls import resource_url
from workgate.oauth.protocol.token_codec import issue_access_token
from workgate.protocol.credentials import (
    executor_credential_verifier,
    new_executor_credential,
)
from workgate.protocol.ids import new_executor_id

PREFIX = "/api/ui/chat"


@pytest.mark.asyncio
async def test_policy_http_inheritance_and_scope_ceiling(tmp_path):
    runtime = runtime_for(tmp_path, auth_mode="oauth")
    db = provider(runtime)
    repo = WorkspaceRepository(db)
    app = build_http_app(runtime=runtime)
    async with (
        runtime.lifespan(),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client,
    ):
        machine = new_executor_id()
        runtime.control_state.put_executor(
            ExecutorTrustRecord(
                executor_id=machine,
                name="offline executor",
                credential_verifier=executor_credential_verifier(
                    new_executor_credential()
                ),
                created_at=10,
            )
        )
        with control_execution_context(
            config=runtime.config,
            state_store=runtime.state_store,
            oauth_state=runtime.oauth_state,
        ):

            def headers(owner, scope):
                return {
                    "Authorization": "Bearer "
                    + issue_access_token(
                        client_id="policy-test",
                        scope=scope,
                        resource=resource_url(),
                        subject=owner,
                    )
                }

            alice = headers("alice", "shell:read shell:write")
            narrow = headers("alice", "shell:read")
            bob = headers("bob", "shell:read shell:write")
        project = repo.create_project("alice", "Research")["id"]
        agent = AgentRepository(db).create(
            "alice",
            {
                "provider": "test-provider",
                "model": "test-model",
                "machine_default_permission": "read",
                "approval_mode": "dangerous",
            },
        )
        cid = repo.create_conversation(
            "alice", {"projectId": project, "agent_id": agent["id"]}
        )["conversationId"]
        global_path = PREFIX + "/settings/permissions"
        project_path = PREFIX + f"/projects/{project}/permissions"
        chat_path = PREFIX + f"/conversations/{cid}/permissions"
        assert (await client.get(global_path, headers=alice)).json() == {
            "machines": {},
            "effective": {machine: "write"},
        }
        assert (
            await client.put(
                global_path,
                json={"machines": {machine: "none"}},
                headers=narrow,
            )
        ).status_code == 403
        assert (
            await client.put(
                global_path,
                json={"machines": {new_executor_id(): "write"}},
                headers=alice,
            )
        ).status_code == 400
        assert (
            await client.put(
                global_path, json={"machines": {machine: "none"}}, headers=alice
            )
        ).json()["effective"] == {machine: "none"}
        assert (
            await client.put(
                project_path, json={"machines": {machine: "write"}}, headers=bob
            )
        ).status_code == 404
        assert (
            await client.put(
                project_path,
                json={"machines": {machine: "write"}},
                headers=alice,
            )
        ).json()["effective"] == {machine: "write"}
        response = await client.get(chat_path, headers=alice)
        assert response.json() == {
            "machines": {},
            "effective": {machine: "write"},
            "authorized": {machine: "read"},
            "agentDefault": "read",
        }
        assert (
            await client.put(
                chat_path, json={"machines": {machine: "none"}}, headers=alice
            )
        ).json()["authorized"] == {machine: "none"}
        assert (
            await client.put(chat_path, json={"machines": {}}, headers=alice)
        ).json()["effective"] == {machine: "write"}
        assert (await client.get(chat_path, headers=bob)).status_code == 404
        approval = PREFIX + "/settings/approval"
        chat_approval = PREFIX + f"/conversations/{cid}/approval"
        assert (await client.get(chat_approval, headers=alice)).json() == {
            "mode": "inherit",
            "effective": "dangerous",
        }
        assert (
            await client.put(approval, json={"mode": "all"}, headers=alice)
        ).json() == {"mode": "all", "effective": "all"}
        assert (
            await client.put(
                chat_approval, json={"mode": "automatic"}, headers=narrow
            )
        ).status_code == 403
        assert (
            await client.put(chat_approval, json={"mode": "all"}, headers=alice)
        ).json() == {"mode": "all", "effective": "all"}
        assert (
            await client.put(
                chat_approval, json={"mode": "inherit"}, headers=alice
            )
        ).json()["effective"] == "dangerous"
        assert (
            await client.put(approval, json={"mode": "inherit"}, headers=alice)
        ).status_code == 400
        assert (await client.get(chat_approval, headers=bob)).status_code == 404
        assert (await client.get(approval, headers=bob)).json()[
            "mode"
        ] == "automatic"
        assert (
            await client.get(
                PREFIX + f"/conversations/{cid}/messages", headers=alice
            )
        ).status_code == 200
