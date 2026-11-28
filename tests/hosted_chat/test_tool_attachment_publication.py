"""Tool attachment links originate from exact owned native checkpoint edges."""

import json

import pytest

from workgate.hosted_chat.agent_runtime import AgentContext, AgentLoopState
from workgate.hosted_chat.attachment_deletion import AttachmentDeletionStore
from workgate.hosted_chat.attachment_history import replay_tool_attachment
from workgate.hosted_chat.attachment_uploads import publish_attachment
from workgate.hosted_chat.database import SQLiteDatabase
from workgate.hosted_chat.generation_actor_lease import ActorLease
from workgate.hosted_chat.generations import GenerationStore
from workgate.hosted_chat.loop_checkpoint import OwnedLoopCheckpoint
from workgate.hosted_chat.provider_common import ModelToolCall, ProviderConfig
from workgate.hosted_chat.storage import WorkspaceRepository
from workgate.hosted_chat.tool_attachment_publication import (
    TOOL_NAME,
    ToolAttachmentPublication,
)


@pytest.fixture
async def publication(tmp_path):
    db = SQLiteDatabase(tmp_path)
    store = GenerationStore(db)
    job, _ = store.begin("alice", {"text": "publish the report"})
    lease = ActorLease.claim(db, "alice", job.run_id)
    assert lease is not None
    checkpoint = OwnedLoopCheckpoint(lease)
    context = AgentContext(
        ProviderConfig("openai", "m", "https://provider.invalid", "secret"), []
    )
    call = ModelToolCall("publish", TOOL_NAME, {"path": "report.txt"})
    state = AgentLoopState(
        context,
        [{"role": "user", "content": "publish the report"}],
        step=1,
        model_turn=1,
        pending_calls=[call],
        tool_step_started=True,
    )
    await checkpoint.save(state, [call])
    return db, store, job, checkpoint, call, state


async def chunks():
    yield b"report"


def binding(checkpoint, call, request=None, *, check=lambda: None):
    return ToolAttachmentPublication(
        checkpoint, call, request or call, check_grant=check
    )


async def publish(db, binder, *, check=lambda: None, owner="alice"):
    return await publish_attachment(
        db,
        owner,
        "report.txt",
        "text/plain",
        chunks(),
        expected_size=6,
        check_grant=check,
        bind=binder.bind,
    )


@pytest.mark.asyncio
async def test_atomic_publication_replay_and_history(publication):
    db, store, job, checkpoint, call, _ = publication
    binder = binding(checkpoint, call)
    assert binder.lookup() is None
    created = await publish(db, binder)
    ident = created["file_id"]
    record = binder.lookup()
    assert record is not None
    assert record["file_id"] == ident
    assert record["toolCallId"] == call.call_id
    assert record["messageId"] == job.response_message_id
    assert record["conversationId"] == job.conversation_id
    assert "path" not in record
    assert "report" not in record["filepath"]
    with pytest.raises(PermissionError, match="already settled"):
        await publish(db, binder)
    with db.read_snapshot() as conn:
        assert (
            conn.execute("SELECT count(*) FROM attachments").fetchone()[0] == 1
        )
        assert (
            conn.execute("SELECT count(*) FROM attachment_uploads").fetchone()[
                0
            ]
            == 0
        )
    checkpoint.lease.release()
    fresh = ActorLease.claim(db, "alice", job.run_id)
    assert fresh is not None
    recovered = OwnedLoopCheckpoint(fresh)
    assert (
        recovered.restore(
            AgentContext(
                ProviderConfig(
                    "openai", "m", "https://provider.invalid", "secret"
                ),
                [],
            )
        )
        is not None
    )
    assert binding(recovered, call).lookup() == record
    events = [
        event
        for _, event in store.events("alice", job.run_id)
        if event.get("event") == "attachment"
    ]
    assert events == [{"event": "attachment", "data": record}]
    with db.read_snapshot() as conn:
        assert (
            replay_tool_attachment(conn, "alice", job.run_id, events[0])
            == events[0]
        )
        assert (
            replay_tool_attachment(conn, "bob", job.run_id, events[0]) is None
        )
        assert (
            replay_tool_attachment(
                conn,
                "alice",
                job.run_id,
                {
                    "event": "attachment",
                    "data": {**record, "toolCallId": "forged"},
                },
            )
            is None
        )
        assert (
            replay_tool_attachment(
                conn,
                "alice",
                job.run_id,
                {
                    "event": "attachment",
                    "data": {**record, "conversationId": "forged"},
                },
            )
            is None
        )
    with db.connect() as conn:
        conn.execute(
            "UPDATE attachments SET name='renamed.txt' WHERE id=?", (ident,)
        )
    with db.read_snapshot() as conn:
        projected = replay_tool_attachment(conn, "alice", job.run_id, events[0])
        assert (
            projected is not None
            and projected["data"]["filename"] == "renamed.txt"
        )
    with db.connect() as conn:
        conn.execute(
            "UPDATE attachments SET name='report.txt' WHERE id=?", (ident,)
        )
    assert store.events("bob", job.run_id) == []
    assert store.finish(
        "alice", job.run_id, status="complete", actor_lease=fresh
    )
    history = WorkspaceRepository(db).messages("alice", job.conversation_id)[
        "messages"
    ]
    reply = next(
        item for item in history if item["messageId"] == job.response_message_id
    )
    assert reply["attachments"] == [record]
    deleted = AttachmentDeletionStore(db).delete(
        "alice", [ident], check_grant=lambda: None
    )
    assert deleted["deletedFileIds"] == [ident]
    history = WorkspaceRepository(db).messages("alice", job.conversation_id)[
        "messages"
    ]
    reply = next(
        item for item in history if item["messageId"] == job.response_message_id
    )
    assert reply.get("attachments", []) == []
    with db.read_snapshot() as conn:
        assert (
            replay_tool_attachment(conn, "alice", job.run_id, events[0]) is None
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode",
    [
        "foreign",
        "forged",
        "edited",
        "revoked",
        "stale",
        "deleted",
        "tampered",
        "pending",
    ],
)
async def test_publication_fences(publication, mode):
    db, _, _, checkpoint, call, state = publication
    binder = binding(checkpoint, call)
    if mode in {"deleted", "tampered", "pending", "edited"}:
        ident = (await publish(db, binder))["file_id"]
        with db.connect() as conn:
            if mode == "deleted":
                conn.execute("DELETE FROM attachments WHERE id=?", (ident,))
            elif mode == "tampered":
                from pathlib import Path

                path = conn.execute(
                    "SELECT path FROM attachments WHERE id=?", (ident,)
                ).fetchone()[0]
                Path(path).write_bytes(b"forged")
            elif mode == "pending":
                path = conn.execute(
                    "SELECT path FROM attachments WHERE id=?", (ident,)
                ).fetchone()[0]
                conn.execute(
                    "INSERT INTO attachment_deletions(file_id,owner,original_path,quarantine_path,phase) VALUES(?,?,?,?,?)",
                    (ident, "alice", path, path + ".delete", "prepared"),
                )
        if mode == "edited":
            binder = binding(
                checkpoint,
                call,
                ModelToolCall(
                    call.call_id, TOOL_NAME, {"path": "different.txt"}
                ),
            )
        with pytest.raises((PermissionError, OSError)):
            binder.lookup()
        return
    if mode == "forged":
        binder = binding(
            checkpoint, ModelToolCall("fake", TOOL_NAME, call.arguments)
        )
    elif mode == "stale":
        state.pending_calls = [ModelToolCall("new", TOOL_NAME, call.arguments)]
        await checkpoint.save(state, state.pending_calls)
    elif mode == "revoked":

        def denied():
            raise PermissionError("revoked")

        binder = binding(checkpoint, call, check=denied)
    with pytest.raises(PermissionError):
        await publish(db, binder, owner="bob" if mode == "foreign" else "alice")
    with db.read_snapshot() as conn:
        for table in (
            "attachments",
            "attachment_uploads",
            "tool_attachment_links",
        ):
            assert (
                conn.execute("SELECT count(*) FROM " + table).fetchone()[0] == 0
            )
        run = conn.execute(
            "SELECT state FROM runs WHERE id=?", (checkpoint.lease.run_id,)
        ).fetchone()
        assert "toolAttachmentPublications" not in json.loads(run["state"])


@pytest.mark.asyncio
async def test_generic_native_call_cannot_create_publication(publication):
    _, _, _, checkpoint, call, _ = publication
    with pytest.raises(PermissionError, match="explicit public root"):
        binding(checkpoint, ModelToolCall(call.call_id, "read", call.arguments))


@pytest.mark.asyncio
async def test_sse_replay_uses_live_attachment_not_historical_snapshot(
    tmp_path,
):
    import httpx

    from tests.support.hosted_chat_http import runtime_for
    from workgate.control.http.app import build_http_app

    runtime = runtime_for(tmp_path)
    db = SQLiteDatabase(runtime.config.state_dir)
    store = GenerationStore(db)
    async with (
        runtime.lifespan(),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=build_http_app(runtime=runtime)),
            base_url="http://testserver",
        ) as client,
    ):
        owner = "local-user"
        job, _ = store.begin(
            owner,
            {"text": "report"},
            native_admission={"scopes": ["shell:read"], "task_machine": None},
        )
        lease = ActorLease.claim(db, owner, job.run_id)
        assert lease is not None
        checkpoint = OwnedLoopCheckpoint(lease)
        context = AgentContext(
            ProviderConfig("openai", "m", "https://provider.invalid", "secret"),
            [],
        )
        call = ModelToolCall("publish", TOOL_NAME, {"path": "report.txt"})
        state = AgentLoopState(
            context,
            [{"role": "user", "content": "report"}],
            step=1,
            model_turn=1,
            pending_calls=[call],
            tool_step_started=True,
        )
        await checkpoint.save(state, [call])
        ident = (await publish(db, binding(checkpoint, call), owner=owner))[
            "file_id"
        ]
        assert store.finish(
            owner, job.run_id, status="complete", actor_lease=lease
        )
        url = "/api/ui/chat/generations/" + job.run_id + "/events"
        response = await client.get(url)
        assert response.status_code == 200
        assert ident in response.text and "report.txt" in response.text
        with db.connect() as conn:
            conn.execute(
                "UPDATE attachments SET name='live.txt' WHERE id=?", (ident,)
            )
        response = await client.get(url)
        assert "live.txt" in response.text and "report.txt" not in response.text
        assert AttachmentDeletionStore(db).delete(
            owner, [ident], check_grant=lambda: None
        )["deletedFileIds"] == [ident]
        response = await client.get(url)
        assert ident not in response.text and '"final":true' in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode",
    [
        "valid",
        "malformed",
        "tampered",
        "deleted",
        "unlinked",
        "delete_during",
        "revoke_during",
    ],
)
async def test_private_office_preview_is_bound_and_preserves_download(
    publication, monkeypatch, mode
):
    import hashlib
    from pathlib import Path

    from workgate.control.http.hosted_chat_attachments import _office_preview
    from workgate.hosted_chat.attachment_history import file_record

    db, store, job, checkpoint, call, _ = publication
    data = b"Name,Value\n<script>alert(1)</script>,42\n"
    name = "report.csv"
    if mode == "malformed":
        data = b"not a ZIP"
        name = "report.docx"

    async def content():
        yield data

    binder = binding(checkpoint, call)
    created = await publish_attachment(
        db,
        "alice",
        name,
        "application/octet-stream",
        content(),
        expected_size=len(data),
        check_grant=lambda: None,
        bind=None if mode == "unlinked" else binder.bind,
    )
    ident = created["file_id"]
    with db.read_snapshot() as conn:
        row = conn.execute(
            "SELECT * FROM attachments WHERE id=?", (ident,)
        ).fetchone()
        assert row is not None
        path = Path(row["path"])
        assert "previewUrl" not in file_record(row)

    if mode != "unlinked":
        record = binder.lookup()
        assert record is not None
        assert record["previewUrl"] == "/api/ui/chat/files/preview/" + ident
        events = [
            event
            for _, event in store.events("alice", job.run_id)
            if event.get("event") == "attachment"
        ]
        assert events[0]["data"]["previewUrl"] == record["previewUrl"]
        assert "<script>" not in json.dumps(events)
    with pytest.raises(KeyError):
        await _office_preview(db, "bob", ident, lambda: None)
    if mode == "tampered":
        path.write_bytes(b"x" * len(data))
    if mode == "deleted":
        AttachmentDeletionStore(db).delete(
            "alice", [ident], check_grant=lambda: None
        )
    if mode in {"tampered", "deleted", "unlinked"}:
        with pytest.raises(KeyError):
            await _office_preview(db, "alice", ident, lambda: None)
        return
    if mode in {"delete_during", "revoke_during"}:
        from workgate.control.http import hosted_chat_attachments as routes

        revoked = False
        render = routes.buffer_to_office_html

        def race(*args):
            nonlocal revoked
            if mode == "delete_during":
                AttachmentDeletionStore(db).delete(
                    "alice", [ident], check_grant=lambda: None
                )
            else:
                revoked = True
            return render(*args)

        def check():
            if revoked:
                raise PermissionError("revoked")

        monkeypatch.setattr(routes, "buffer_to_office_html", race)
        with pytest.raises(
            KeyError if mode == "delete_during" else PermissionError
        ):
            await _office_preview(db, "alice", ident, check)
        return
    response = await _office_preview(db, "alice", ident, lambda: None)
    assert response.headers["cache-control"] == "no-store"
    assert "sandbox;" in response.headers["content-security-policy"]
    assert path.read_bytes() == data
    assert (
        hashlib.sha256(path.read_bytes()).hexdigest()
        == hashlib.sha256(data).hexdigest()
    )
    if mode == "malformed":
        assert response.status_code == 422
    else:
        assert response.status_code == 200
        assert b"&lt;script&gt;" in response.body
        assert b"<script>" not in response.body
        assert response.headers["content-type"].startswith("text/html")


@pytest.mark.asyncio
async def test_office_preview_http_uses_owner_read_scope_and_original_download(
    tmp_path,
):
    import httpx

    from tests.support.hosted_chat_http import runtime_for
    from workgate.control.execution_context import control_execution_context
    from workgate.control.http.app import build_http_app
    from workgate.oauth.core.urls import resource_url
    from workgate.oauth.protocol.token_codec import issue_access_token

    runtime = runtime_for(tmp_path, auth_mode="oauth")
    db = SQLiteDatabase(runtime.config.state_dir)
    store = GenerationStore(db)
    job, _ = store.begin("alice", {"text": "publish"})
    lease = ActorLease.claim(db, "alice", job.run_id)
    assert lease is not None
    checkpoint = OwnedLoopCheckpoint(lease)
    context = AgentContext(
        ProviderConfig("openai", "m", "https://provider.invalid", "secret"), []
    )
    call = ModelToolCall("publish", TOOL_NAME, {"path": "data.csv"})
    state = AgentLoopState(
        context,
        [{"role": "user", "content": "publish"}],
        step=1,
        model_turn=1,
        pending_calls=[call],
        tool_step_started=True,
    )
    await checkpoint.save(state, [call])
    original = b"x,y\n<a>,2\n"

    async def data():
        yield original

    created = await publish_attachment(
        db,
        "alice",
        "data.csv",
        "text/csv",
        data(),
        expected_size=len(original),
        check_grant=lambda: None,
        bind=binding(checkpoint, call).bind,
    )
    ident = created["file_id"]
    assert store.finish(
        "alice", job.run_id, status="complete", text="ready", actor_lease=lease
    )
    lease.release()
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

            def token(owner, scope):
                return {
                    "Authorization": "Bearer "
                    + issue_access_token(
                        client_id="office-test",
                        scope=scope,
                        resource=resource_url(),
                        subject=owner,
                    )
                }

            alice = token("alice", "shell:read")
            bob = token("bob", "shell:read")
            narrow = token("alice", "audit:read")
        url = "/api/ui/chat/files/preview/" + ident
        assert (await client.get(url)).status_code == 401
        assert (await client.get(url, headers=bob)).status_code == 404
        assert (await client.get(url, headers=narrow)).status_code == 403
        response = await client.get(url, headers=alice)
        assert response.status_code == 200, response.text
        assert "sandbox;" in response.headers["content-security-policy"]
        assert (
            "default-src 'none'" in response.headers["content-security-policy"]
        )
        assert response.headers["x-content-type-options"] == "nosniff"
        assert "&lt;a&gt;" in response.text
        download = await client.get(
            "/api/ui/chat/files/download/" + ident, headers=alice
        )
        assert download.content == original
        assert download.headers["content-type"] == "application/octet-stream"
