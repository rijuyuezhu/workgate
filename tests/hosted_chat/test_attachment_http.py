"""Native attachment routes retain owner and scope fences."""

import httpx
import pytest

from tests.support.hosted_chat_http import runtime_for
from workgate.control.execution_context import control_execution_context
from workgate.control.http.app import build_http_app
from workgate.hosted_chat.database import SQLiteDatabase
from workgate.oauth.core.urls import resource_url
from workgate.oauth.protocol.token_codec import issue_access_token

PREFIX = "/api/ui/chat/files"


@pytest.mark.asyncio
async def test_attachment_http_owner_and_write_scope(tmp_path):
    runtime = runtime_for(tmp_path, auth_mode="oauth")
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
                        client_id="attachment-test",
                        scope=scope,
                        resource=resource_url(),
                        subject=owner,
                    )
                }

            alice, bob, read = (
                headers("alice"),
                headers("bob"),
                headers("alice", "shell:read"),
            )
            files = {"file": ("hello.txt", b"hello", "text/plain")}
            assert (await client.get(PREFIX)).status_code == 401
            assert (
                await client.post(PREFIX, files=files, headers=read)
            ).status_code == 403
            created = await client.post(PREFIX, files=files, headers=alice)
            assert created.status_code == 201, created.text
            record = created.json()
            ident, path = record["file_id"], record["filepath"]
            assert record["bytes"] == 5
            assert "owner" not in record and "path" not in record
            assert path == PREFIX + "/download/" + ident
            assert (await client.get(PREFIX, headers=bob)).json() == []
            assert (await client.get(PREFIX, headers=read)).json() == [record]
            assert (await client.get(path, headers=bob)).status_code == 404
            downloaded = await client.get(path, headers=read)
            assert downloaded.content == b"hello"
            assert (
                downloaded.headers["content-type"] == "application/octet-stream"
            )
            assert downloaded.headers["x-content-type-options"] == "nosniff"
            assert downloaded.headers["cache-control"] == "no-store"
            assert "attachment;" in downloaded.headers["content-disposition"]
            body = {"file_ids": [ident]}
            assert (
                await client.request("DELETE", PREFIX, json=body, headers=read)
            ).status_code == 403
            assert (
                await client.request("DELETE", PREFIX, json=body, headers=bob)
            ).status_code == 403
            deleted = await client.request(
                "DELETE", PREFIX, json=body, headers=alice
            )
            assert deleted.json() == {
                "deletedFileIds": [ident],
                "failedFileIds": [],
            }
            assert (await client.get(path, headers=alice)).status_code == 404
            assert (await client.get(PREFIX, headers=alice)).json() == []


@pytest.mark.asyncio
async def test_attachment_http_validation_and_private_path(
    tmp_path, monkeypatch
):
    runtime = runtime_for(tmp_path)
    async with (
        runtime.lifespan(),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=build_http_app(runtime=runtime)),
            base_url="http://testserver",
        ) as client,
    ):
        assert (await client.post(PREFIX, json={})).status_code == 400
        assert (
            await client.post(PREFIX, files={"wrong": ("a", b"a")})
        ).status_code == 400
        assert (
            await client.post(PREFIX, files={"file": ("a", b"")})
        ).status_code == 400
        assert (
            await client.post(
                PREFIX, files=[("file", ("a", b"a")), ("file", ("b", b"b"))]
            )
        ).status_code == 400
        assert (
            await client.post(
                PREFIX, files={"file": ("a", b"a")}, data={"agent_id": "a"}
            )
        ).status_code == 400
        assert (
            await client.post(
                PREFIX,
                content=b"oops",
                headers={"Content-Type": "multipart/form-data"},
            )
        ).status_code == 400
        assert (
            await client.request("DELETE", PREFIX, json={"file_ids": "abc"})
        ).status_code == 400
        assert (
            await client.request("DELETE", PREFIX, json={"file_ids": [None]})
        ).status_code == 400
        assert (await client.get(PREFIX)).json() == []
        from workgate.control.http import hosted_chat_attachments

        monkeypatch.setattr(hosted_chat_attachments, "MAX_ATTACHMENT_BYTES", 4)
        oversized = await client.post(
            PREFIX, files={"file": ("a", b"x" * 66000)}
        )
        assert oversized.status_code == 400
        created = await client.post(PREFIX, files={"file": ("a", b"abc")})
        assert created.status_code == 201
        path = created.json()["filepath"]
        db = SQLiteDatabase(runtime.config.state_dir)
        outside = tmp_path / "secret"
        outside.write_bytes(b"secret")
        with db.connect() as conn:
            conn.execute("UPDATE attachments SET path=?", (str(outside),))
        assert (await client.get(path)).status_code == 404
        assert outside.read_bytes() == b"secret"


@pytest.mark.asyncio
async def test_attachment_routes_disabled(tmp_path):
    runtime = runtime_for(tmp_path, enabled=False)
    async with (
        runtime.lifespan(),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=build_http_app(runtime=runtime)),
            base_url="http://testserver",
        ) as client,
    ):
        assert (await client.get(PREFIX)).status_code == 404
