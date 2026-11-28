"""Agent context file list/detach routes preserve owner and revision fences."""

import json

import httpx
import pytest

from tests.support.hosted_chat_http import runtime_for
from workgate.control.execution_context import control_execution_context
from workgate.control.http.app import build_http_app
from workgate.hosted_chat.agents import AgentRepository
from workgate.hosted_chat.database import SQLiteDatabase
from workgate.oauth.core.urls import resource_url
from workgate.oauth.protocol.token_codec import issue_access_token

PREFIX = "/api/ui/chat/files"


def agent(repo, owner="alice"):
    return repo.create(owner, {"provider": "openai", "model": None})


@pytest.mark.asyncio
async def test_list_and_detach_shared_agent_file(tmp_path):
    runtime = runtime_for(tmp_path, auth_mode="oauth")
    database = SQLiteDatabase(runtime.config.state_dir)
    repo = AgentRepository(database)
    aid = agent(repo)["id"]
    path = PREFIX + "/agent/" + aid
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

            def headers(owner, scope="shell:read shell:write"):
                return {
                    "Authorization": "Bearer "
                    + issue_access_token(
                        client_id="resource-http-test",
                        scope=scope,
                        resource=resource_url(),
                        subject=owner,
                    )
                }

            alice, bob = headers("alice"), headers("bob")
            read = headers("alice", "shell:read")
        assert (await client.get(path)).status_code == 401
        assert (await client.get(path, headers=bob)).status_code == 404
        assert (await client.get(path, headers=read)).json() == []
        uploaded = await client.post(
            path,
            headers=alice,
            files={"file": ("a.txt", b"private text", "text/plain")},
        )
        assert uploaded.status_code == 201
        record = uploaded.json()
        ident = record["file_id"]
        clone = repo.duplicate("alice", aid)
        assert clone is not None
        clone_path = PREFIX + "/agent/" + clone["agent"]["id"]
        assert (await client.get(clone_path, headers=read)).json() == [record]
        listing = await client.get(path, headers=read)
        assert listing.json() == [record]
        assert "private text" not in listing.text
        body = {"file_ids": [ident]}
        assert (
            await client.request("DELETE", path, json=body, headers=read)
        ).status_code == 403
        assert (
            await client.request("DELETE", path, json=body, headers=bob)
        ).status_code == 404
        for invalid in (
            {"file_ids": "bad"},
            {"file_ids": [None]},
            {"file_ids": [ident] * 101},
        ):
            assert (
                await client.request(
                    "DELETE", path, json=invalid, headers=alice
                )
            ).status_code == 400
        detached = await client.request(
            "DELETE", path, json=body, headers=alice
        )
        assert detached.json() == {"success": True, "version": 3}
        assert (await client.get(path, headers=read)).json() == []
        assert (await client.get(clone_path, headers=read)).json() == [record]
        assert (
            await client.get(record["filepath"], headers=read)
        ).content == b"private text"
        assert (
            await client.request("DELETE", path, json=body, headers=alice)
        ).json() == detached.json()
        guarded = await client.request(
            "DELETE", PREFIX, json=body, headers=alice
        )
        assert guarded.json()["failedFileIds"] == [ident]
        await client.request("DELETE", clone_path, json=body, headers=alice)
        deleted = await client.request(
            "DELETE", PREFIX, json=body, headers=alice
        )
        assert deleted.json()["deletedFileIds"] == [ident]


@pytest.mark.asyncio
async def test_list_skips_foreign_missing_and_pending_file_references(tmp_path):
    runtime = runtime_for(tmp_path)
    database = SQLiteDatabase(runtime.config.state_dir)
    repo = AgentRepository(database)
    aid = agent(repo, owner="local-user")["id"]
    path = PREFIX + "/agent/" + aid
    with database.connect() as db:
        for ident, owner in (
            ("good", "local-user"),
            ("pending", "local-user"),
            ("foreign", "bob"),
        ):
            db.execute(
                "INSERT INTO attachments(id,owner,name,path,content_type,metadata) VALUES(?,?,?,?,?,?)",
                (
                    ident,
                    owner,
                    ident + ".txt",
                    "/private/" + ident,
                    "text/plain",
                    "{}",
                ),
            )
        db.execute(
            "INSERT INTO attachment_deletions(file_id,owner,original_path,quarantine_path,phase) VALUES(?,?,?,?,?)",
            (
                "pending",
                "local-user",
                "/private/pending",
                "/private/.delete-pending",
                "prepared",
            ),
        )
        row = db.execute(
            "SELECT config FROM agents WHERE id=?", (aid,)
        ).fetchone()
        config = json.loads(row["config"])
        config["tool_resources"] = {
            "context": {
                "file_ids": ["foreign", "good", "missing", "pending", "good"]
            }
        }
        db.execute(
            "UPDATE agents SET config=? WHERE id=?", (json.dumps(config), aid)
        )
    async with (
        runtime.lifespan(),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=build_http_app(runtime=runtime)),
            base_url="http://testserver",
        ) as client,
    ):
        result = await client.get(path)
        assert result.status_code == 200
        assert [item["file_id"] for item in result.json()] == ["good"]
        assert "/private/" not in result.text
