"""Native Agent context publication preserves the attachment journal and revision fence."""

import json
from pathlib import Path

import httpx
import pytest

from tests.support.hosted_chat_http import runtime_for
from workgate.control.execution_context import control_execution_context
from workgate.control.http.app import build_http_app
from workgate.hosted_chat.agents import AgentRepository
from workgate.hosted_chat.attachment_uploads import publish_attachment
from workgate.hosted_chat.context_extraction import extract_context_text
from workgate.hosted_chat.database import SQLiteDatabase
from workgate.oauth.core.urls import resource_url
from workgate.oauth.protocol.token_codec import issue_access_token

PREFIX = "/api/ui/chat/files"


async def chunks(data):
    yield data


def agent(repo, owner="alice"):
    return repo.create(owner, {"provider": "openai", "model": None})


def empty_uploads(database):
    with database.connect() as db:
        assert not db.execute("SELECT * FROM attachments").fetchall()
        assert not db.execute("SELECT * FROM attachment_uploads").fetchall()
    assert not list((database.root / "attachments").rglob("file_*"))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", ["bind", "grant", "extract", "agent_deleted"]
)
async def test_atomic_publication_rollback(tmp_path, failure):
    database = SQLiteDatabase(tmp_path)
    repo = AgentRepository(database)
    aid = agent(repo)["id"]
    data = b"reference"
    revoke = False

    def check():
        if revoke:
            raise PermissionError("revoked")

    def process(path, name, mime):
        if failure == "extract":
            raise ValueError("extraction failed")
        if failure == "agent_deleted":
            repo.delete("alice", aid)
        return extract_context_text(path, name, mime)

    def bind(db, ident):
        nonlocal revoke
        repo.add_resource_file("alice", aid, "context", ident, db=db)
        if failure == "bind":
            raise RuntimeError("failed after binding")
        if failure == "grant":
            revoke = True

    expected = {
        "bind": RuntimeError,
        "grant": PermissionError,
        "extract": ValueError,
        "agent_deleted": KeyError,
    }
    with pytest.raises(expected[failure]):
        await publish_attachment(
            database,
            "alice",
            "reference.txt",
            "text/plain",
            chunks(data),
            expected_size=len(data),
            check_grant=check,
            process=process,
            bind=bind,
        )
    empty_uploads(database)
    current = repo.get("alice", aid)
    if failure == "agent_deleted":
        assert current is None
    else:
        assert current is not None
        assert current["version"] == 1
        assert "tool_resources" not in current
        assert len(repo.versions("alice", aid) or []) == 1


@pytest.mark.asyncio
async def test_changed_bytes_before_processing_never_reach_extractor(tmp_path):
    database = SQLiteDatabase(tmp_path)
    calls = 0

    def check():
        nonlocal calls
        calls += 1
        if calls == 3:
            next((database.root / "attachments").rglob("file_*")).write_bytes(
                b"bad"
            )

    def process(*_args):
        pytest.fail("changed bytes must not reach extractor")

    with pytest.raises(OSError, match="changed before processing"):
        await publish_attachment(
            database,
            "alice",
            "test.txt",
            "text/plain",
            chunks(b"abc"),
            expected_size=3,
            check_grant=check,
            process=process,
        )
    with database.connect() as db:
        assert not db.execute("SELECT * FROM attachments").fetchall()
        assert db.execute("SELECT * FROM attachment_uploads").fetchone()
    assert (
        next((database.root / "attachments").rglob("file_*")).read_bytes()
        == b"bad"
    )


@pytest.mark.asyncio
async def test_agent_upload_route_scope_ownership_and_metadata(tmp_path):
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
                        client_id="resource-test",
                        scope=scope,
                        resource=resource_url(),
                        subject=owner,
                    )
                }

            alice = headers("alice")
            read = headers("alice", "shell:read")
            bob = headers("bob")
        files = {"file": ("reference.txt", b"private evidence", "text/plain")}
        assert (await client.post(path, files=files)).status_code == 401
        assert (
            await client.post(path, files=files, headers=read)
        ).status_code == 403
        assert (
            await client.post(path, files=files, headers=bob)
        ).status_code == 404
        unsupported = await client.post(
            path,
            files={"file": ("a.png", b"image", "image/png")},
            headers=alice,
        )
        assert unsupported.status_code == 501
        invalid = await client.post(
            path,
            files={"file": ("a.txt", b"\xff", "text/plain")},
            headers=alice,
        )
        assert invalid.status_code == 415
        empty_uploads(database)
        created = await client.post(path, files=files, headers=alice)
        assert created.status_code == 201, created.text
        record = created.json()
        ident = record["file_id"]
        assert record["processed"] is True
        assert record["extraction"] == "utf8"
        assert "private evidence" not in created.text
        current = repo.get("alice", aid)
        assert current is not None
        assert current["version"] == 2
        assert current["tool_resources"]["context"]["file_ids"] == [ident]
        with database.connect() as db:
            row = db.execute(
                "SELECT * FROM attachments WHERE id=?", (ident,)
            ).fetchone()
            assert Path(row["path"]).read_bytes() == b"private evidence"
            assert (
                json.loads(row["metadata"])["extracted_text"]
                == "private evidence"
            )
            assert not db.execute("SELECT * FROM attachment_uploads").fetchall()
        listing = await client.get(PREFIX, headers=alice)
        assert listing.json() == [record]
        assert "private evidence" not in listing.text
        assert (
            await client.get(record["filepath"], headers=bob)
        ).status_code == 404
        assert (
            await client.get(record["filepath"], headers=read)
        ).content == b"private evidence"
        denied = await client.request(
            "DELETE", PREFIX, json={"file_ids": [ident]}, headers=alice
        )
        assert denied.json()["failedFileIds"] == [ident]


@pytest.mark.asyncio
async def test_recovery_during_extraction_cannot_publish_missing_bytes(
    tmp_path,
):
    from workgate.hosted_chat.attachment_uploads import (
        recover_attachment_uploads,
    )

    database = SQLiteDatabase(tmp_path)
    repo = AgentRepository(database)
    aid = agent(repo)["id"]

    def process(path, name, mime):
        result = extract_context_text(path, name, mime)
        assert recover_attachment_uploads(database, stale_seconds=0) == 1
        return result

    def bind(db, ident):
        repo.add_resource_file("alice", aid, "context", ident, db=db)

    with pytest.raises(OSError):
        await publish_attachment(
            database,
            "alice",
            "a.txt",
            "text/plain",
            chunks(b"abc"),
            expected_size=3,
            check_grant=lambda: None,
            process=process,
            bind=bind,
        )
    empty_uploads(database)
    current = repo.get("alice", aid)
    assert current is not None and current["version"] == 1


@pytest.mark.asyncio
async def test_live_agent_policy_blocks_atomic_binding(tmp_path):
    from workgate.hosted_chat.storage import WorkspaceRepository

    runtime = runtime_for(tmp_path)
    database = SQLiteDatabase(runtime.config.state_dir)
    repo = AgentRepository(database)
    aid = repo.create(
        "local-user",
        {
            "provider": "openai",
            "model": None,
            "instructions": "Bearer private-token",
        },
    )["id"]
    WorkspaceRepository(database).save_setting(
        "local-user",
        "webui.filters.config",
        {
            "agentInstructions": {
                "pii": {"action": "block", "starterPatterns": ["bearer_header"]}
            },
        },
    )
    async with (
        runtime.lifespan(),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=build_http_app(runtime=runtime)),
            base_url="http://testserver",
        ) as client,
    ):
        denied = await client.post(
            PREFIX + "/agent/" + aid,
            files={"file": ("a.txt", b"reference", "text/plain")},
        )
        assert denied.status_code == 400
        assert denied.json()["error"] == "content_filter_block"
        assert "private-token" not in denied.text
        empty_uploads(database)
        current = repo.get("local-user", aid)
        assert current is not None and current["version"] == 1
