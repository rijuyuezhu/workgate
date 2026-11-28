"""Protected Skill authoring and read-only installed Skill projection."""

from dataclasses import replace

import httpx
import pytest

from tests.support.hosted_chat_http import runtime_for
from workgate.control.execution_context import control_execution_context
from workgate.control.http.app import build_http_app
from workgate.oauth.core.urls import resource_url
from workgate.oauth.protocol.token_codec import issue_access_token
from workgate.schemas.result_models.agent import (
    ActivateAgentSkillOutput,
    ListAgentSkillsOutput,
    ReadAgentSkillFileOutput,
)

PREFIX = "/api/ui/chat/skills"


@pytest.mark.asyncio
async def test_skill_http_owner_tree_and_readonly_catalog(
    tmp_path, monkeypatch
):
    runtime = runtime_for(tmp_path, auth_mode="oauth")
    runtime.config = replace(runtime.config, agent_bridge_enabled=True)

    async def installed(session_id):
        assert session_id is None
        return ListAgentSkillsOutput(
            sources=[],
            skills=[
                {
                    "name": "installed",
                    "description": "Installed Skill",
                    "related_files": ["refs.txt"],
                }
            ],
            warnings=[],
        )

    async def activate(session_id, name):
        assert session_id is None and name == "installed"
        return ActivateAgentSkillOutput(
            name=name,
            source="control",
            source_path="/private/path",
            entry_path="SKILL.md",
            description="Installed Skill",
            content="Installed text",
            bytes=14,
            related_files=["refs.txt"],
        )

    async def read(session_id, name, path):
        assert session_id is None and name == "installed" and path == "refs.txt"
        return ReadAgentSkillFileOutput(
            name=name,
            source="control",
            source_path="/private/path",
            path=path,
            content="Reference",
            bytes=9,
        )

    monkeypatch.setattr(runtime.agent_bridge, "list_skills", installed)
    monkeypatch.setattr(runtime.agent_bridge, "activate_skill", activate)
    monkeypatch.setattr(runtime.agent_bridge, "read_skill_file", read)
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
                        client_id="skills-test",
                        scope=scope,
                        resource=resource_url(),
                        subject=owner,
                    )
                }

            alice = headers("alice", "shell:read shell:write")
            bob = headers("bob", "shell:read shell:write")
            reader = headers("alice", "shell:read")
        assert (await client.get(PREFIX)).status_code == 401
        payload = {
            "name": "review",
            "description": "Review",
            "body": "# Review",
        }
        assert (
            await client.post(PREFIX, headers=reader, json=payload)
        ).status_code == 403
        created = await client.post(PREFIX, headers=alice, json=payload)
        assert created.status_code == 201, created.text
        sid = created.json()["_id"]
        url = PREFIX + "/" + sid
        assert (await client.get(url, headers=bob)).status_code == 404
        assert (await client.get(url, headers=reader)).json()[
            "body"
        ] == "# Review"
        listed = (await client.get(PREFIX, headers=reader)).json()["skills"]
        assert {s["_id"] for s in listed} == {sid, "deployment:installed"}
        assert (
            await client.get(PREFIX, headers=reader, params={"q": "installed"})
        ).json()["skills"][0]["_id"] == "deployment:installed"
        assert (
            await client.get(PREFIX, headers=reader, params={"limit": 0})
        ).status_code == 400
        assert (
            await client.get(PREFIX, headers=reader, params={"limit": 1})
        ).json()["after"] == "1"
        deployment = PREFIX + "/deployment:installed"
        assert (await client.get(deployment, headers=reader)).json()[
            "body"
        ] == "Installed text"
        assert (
            await client.get(deployment + "/files/refs.txt", headers=reader)
        ).json()["content"] == "Reference"
        assert (
            "/private/path"
            not in (
                await client.get(deployment + "/files/SKILL.md", headers=reader)
            ).text
        )
        assert (
            await client.patch(
                deployment, headers=alice, json={"expectedVersion": 1}
            )
        ).status_code == 403
        assert (
            await client.get(deployment + "/tree", headers=reader)
        ).status_code == 403
        assert (
            await client.post(
                PREFIX + "/states",
                headers=reader,
                json={"skillStates": {sid: False}},
            )
        ).status_code == 403
        assert (
            await client.post(
                PREFIX + "/states",
                headers=alice,
                json={"skillStates": {sid: False}},
            )
        ).json() == {sid: False}
        assert (
            await client.get(PREFIX + "/states", headers=reader)
        ).json() == {sid: False}
        tree = url + "/tree"
        assert (await client.get(tree, headers=reader)).json() == {
            "version": 1,
            "nodes": [],
        }
        folder = (
            await client.post(
                tree,
                headers=alice,
                json={"type": "folder", "path": "refs", "expectedVersion": 1},
            )
        ).json()
        assert folder["skillVersion"] == 2
        item = (
            await client.post(
                tree,
                headers=alice,
                json={
                    "type": "file",
                    "path": "refs/a.txt",
                    "content": "first",
                    "expectedVersion": 2,
                },
            )
        ).json()
        assert item["skillVersion"] == 3
        assert (
            await client.post(
                tree,
                headers=alice,
                json={"type": "file", "path": "bad", "expectedVersion": 1},
            )
        ).status_code == 409
        file = url + "/files/refs/a.txt"
        assert (await client.get(file, headers=reader)).json()[
            "content"
        ] == "first"
        updated = await client.put(
            file,
            headers=alice,
            json={"content": "second", "expectedVersion": 3},
        )
        assert updated.status_code == 200
        raw = await client.get(file, headers=reader, params={"raw": "true"})
        assert (
            raw.content == b"second"
            and raw.headers["content-disposition"] == "attachment"
        )
        assert raw.headers["x-content-type-options"] == "nosniff"
        assert (
            await client.patch(
                tree + "/" + item["id"],
                headers=alice,
                json={"path": "refs/b.txt", "expectedVersion": 4},
            )
        ).status_code == 200
        binary = await client.post(
            tree,
            headers=alice,
            json={
                "type": "file",
                "path": "binary",
                "base64": "/wA=",
                "expectedVersion": 5,
            },
        )
        assert binary.status_code == 201
        assert (await client.get(url + "/files/binary", headers=reader)).json()[
            "isBinary"
        ] is True
        assert (
            await client.request(
                "DELETE",
                tree + "/" + folder["id"],
                headers=alice,
                json={"expectedVersion": 6},
            )
        ).status_code == 200
        assert (
            await client.get(url + "/files/refs/b.txt", headers=reader)
        ).status_code == 404
        main = await client.get(url + "/files/SKILL.md", headers=reader)
        assert "name: review" in main.json()["content"]
        assert (
            await client.patch(
                url, headers=alice, json={"expectedVersion": 7, "body": "New"}
            )
        ).status_code == 200
        assert (await client.delete(url, headers=bob)).status_code == 404
        assert (await client.delete(url, headers=alice)).status_code == 200
