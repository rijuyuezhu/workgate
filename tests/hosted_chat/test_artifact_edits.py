"""Authenticated artifact mutation, source parity, and durable replay fences."""

import json

import httpx
import pytest

from tests.support.hosted_chat_http import runtime_for
from workgate.control.execution_context import control_execution_context
from workgate.control.http.app import build_http_app
from workgate.hosted_chat.artifact_edits import (
    ArtifactConflict,
    update_artifact,
)
from workgate.hosted_chat.database import SQLiteDatabase
from workgate.hosted_chat.generation_actor_lease import ActorLease
from workgate.hosted_chat.generations import (
    GenerationStore,
    generation_model_history,
)
from workgate.hosted_chat.storage import WorkspaceRepository
from workgate.oauth.core.urls import resource_url
from workgate.oauth.protocol.token_codec import issue_access_token

FIRST = (
    'before 😀\n:::artifact{identifier="first"}\n```html\n<p>old</p>\n```\n:::'
)
SECOND = '\n:::artifact{identifier="second"}\n```text\nold\n```\n:::'
PREFIX = "/api/ui/chat/messages/artifact/"


def terminal(database, owner="alice"):
    store = GenerationStore(database)
    run, _ = store.begin(owner, {"text": "create two artifacts"})
    lease = ActorLease.claim(database, owner, run.run_id)
    assert lease is not None
    steps = [
        {
            "role": "assistant",
            "content": FIRST,
            "tool_calls": [
                {
                    "id": "c",
                    "type": "function",
                    "function": {"name": "task", "arguments": "{}"},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "c",
            "content": "unchanged tool result",
        },
        {"role": "assistant", "content": SECOND},
    ]
    assert store.finish(
        owner,
        run.run_id,
        status="complete",
        text=FIRST + SECOND,
        model_steps=steps,
        actor_lease=lease,
    )
    lease.release()
    return store, run, steps


@pytest.fixture
def finished(tmp_path):
    database = SQLiteDatabase(tmp_path)
    store, run, steps = terminal(database)
    return database, store, run, steps


def snapshot(database, run):
    with database.read_snapshot() as db:
        return tuple(
            tuple(row)
            for row in (
                db.execute(
                    "SELECT text,content,metadata FROM messages WHERE id=?",
                    (run.response_message_id,),
                ).fetchone(),
                db.execute(
                    "SELECT state FROM runs WHERE id=?", (run.run_id,)
                ).fetchone(),
                db.execute(
                    "SELECT data FROM generation_events WHERE run_id=? AND json_extract(data,'$.final')=1",
                    (run.run_id,),
                ).fetchone(),
            )
        )


def edit(
    database,
    run,
    index=0,
    original="old",
    updated="new 🚀",
    *,
    owner="alice",
    check=lambda: None,
):
    return update_artifact(
        database,
        owner,
        run.response_message_id,
        index,
        original,
        updated,
        check_grant=check,
    )


def test_atomic_edit_preserves_tool_receipts_and_terminal_replay(finished):
    database, store, run, steps = finished
    initial_events = store.events("alice", run.run_id)
    result = edit(database, run, index=1)
    assert result["text"] == FIRST + SECOND.replace("old", "new 🚀")
    assert result["content"][0]["text"] == result["text"]
    result = edit(
        database, run, index=0, original="<p>old</p>", updated="<p>first</p>"
    )
    assert result["text"] == FIRST.replace("old", "first") + SECOND.replace(
        "old", "new 🚀"
    )
    messages = WorkspaceRepository(database).messages(
        "alice", run.conversation_id
    )["messages"]
    reply = next(item for item in messages if item["role"] == "assistant")
    assert reply["userSubmittedPaths"] == ["/content/0/text"]
    events = store.events("alice", run.run_id)
    assert events[:-1] == initial_events[:-1]
    assert events[-1][1]["responseMessage"]["text"] == result["text"]
    assert events[-1][1]["responseMessage"]["userSubmittedPaths"] == [
        "/content/0/text"
    ]
    next_run, _ = store.begin(
        "alice", {"text": "continue", "conversationId": run.conversation_id}
    )
    with database.read_snapshot() as db:
        history = generation_model_history(db, "alice", next_run)
    assert history[1] == {**steps[0], "content": FIRST.replace("old", "first")}
    assert history[2] == steps[1]
    assert history[3] == {
        **steps[2],
        "content": SECOND.replace("old", "new 🚀"),
    }
    assert history[-1] == {"role": "user", "content": "continue"}
    with pytest.raises(ArtifactConflict, match="active work"):
        edit(database, run, original="first")


@pytest.mark.parametrize(
    "mode",
    [
        "foreign",
        "stale",
        "corrupt",
        "terminal",
        "disk",
        "revoked",
        "policy",
        "oversized",
    ],
)
def test_edit_failure_is_atomic(finished, mode):
    database, _, run, _ = finished
    repo = WorkspaceRepository(database)
    if mode == "corrupt":
        with database.connect() as db:
            state = json.loads(
                db.execute(
                    "SELECT state FROM runs WHERE id=?", (run.run_id,)
                ).fetchone()[0]
            )
            state["modelSteps"][0]["content"] = FIRST.replace(
                "first", "different"
            )
            db.execute(
                "UPDATE runs SET state=? WHERE id=?",
                (json.dumps(state), run.run_id),
            )
    elif mode == "terminal":
        with database.connect() as db:
            db.execute(
                "UPDATE generation_events SET data=json_set(data,'$.responseMessage.text','forged') WHERE run_id=? AND json_extract(data,'$.final')=1",
                (run.run_id,),
            )
    elif mode == "disk":
        with database.connect() as db:
            db.execute(
                "CREATE TRIGGER fail_edit BEFORE UPDATE ON messages BEGIN SELECT RAISE(ABORT,'disk failure'); END"
            )
    elif mode == "policy":
        repo.save_setting(
            "alice",
            "webui.filters.config",
            {"messages": {"pii": {"starterPatterns": ["bearer_header"]}}},
        )
    before = snapshot(database, run)
    calls = 0

    def check():
        nonlocal calls
        calls += 1
        if mode == "revoked" and calls > 1:
            raise PermissionError("revoked")

    import sqlite3

    with pytest.raises((KeyError, ValueError, PermissionError, sqlite3.Error)):
        edit(
            database,
            run,
            owner="bob" if mode == "foreign" else "alice",
            original="absent" if mode == "stale" else "old",
            updated="Bearer private"
            if mode == "policy"
            else "x" * 300_000
            if mode == "oversized"
            else "new",
            check=check,
        )
    assert snapshot(database, run) == before


@pytest.mark.parametrize("mode", ["reserved", "registered", "metadata"])
def test_child_threads_are_read_only(tmp_path, mode):
    database = SQLiteDatabase(tmp_path)
    repo = WorkspaceRepository(database)
    payload = (
        {"conversationId": "12345678-1234-8123-b123-123456789abc"}
        if mode == "reserved"
        else {"subagentThread": {"parentConversationId": "parent"}}
        if mode == "metadata"
        else {}
    )
    convo = repo.create_conversation("alice", payload)
    cid = convo["conversationId"]
    message = repo.add_message(
        "alice", cid, {"role": "assistant", "text": FIRST}
    )
    if mode == "registered":
        with database.connect() as db:
            db.execute(
                "INSERT INTO active_subagent_threads(owner,thread_id,parent_run_id,active) VALUES(?,?,?,0)",
                ("alice", cid, "parent"),
            )
    with pytest.raises(ArtifactConflict, match="read-only"):
        update_artifact(
            database,
            "alice",
            message["messageId"],
            0,
            "old",
            "new",
            check_grant=lambda: None,
        )


@pytest.mark.asyncio
async def test_artifact_route_owner_scope_and_no_store(tmp_path):
    runtime = runtime_for(tmp_path, auth_mode="oauth")
    database = SQLiteDatabase(runtime.config.state_dir)
    _, run, _ = terminal(database)
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
                        client_id="artifact-test",
                        scope=scope,
                        resource=resource_url(),
                        subject=owner,
                    )
                }

            alice = token("alice", "shell:read shell:write")
            bob = token("bob", "shell:read shell:write")
            narrow = token("alice", "shell:read")
        url = PREFIX + run.response_message_id
        body = {"index": 0, "original": "<p>old</p>", "updated": "<p>new</p>"}
        assert (await client.post(url, json=body)).status_code == 401
        assert (
            await client.post(url, json=body, headers=bob)
        ).status_code == 404
        assert (
            await client.post(url, json=body, headers=narrow)
        ).status_code == 403
        assert (
            await client.post(url, json={**body, "index": True}, headers=alice)
        ).status_code == 400
        response = await client.post(url, json=body, headers=alice)
        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "no-store"
        assert response.json()["text"] == FIRST.replace("old", "new") + SECOND


@pytest.mark.parametrize("mode", ["text", "content", "ambiguous", "split"])
def test_legacy_edit_keeps_replay_consistent(tmp_path, mode):
    database = SQLiteDatabase(tmp_path)
    repo = WorkspaceRepository(database)
    cid = repo.create_conversation("alice", {})["conversationId"]
    body: dict = {"role": "assistant", "text": FIRST}
    if mode == "content":
        body["content"] = [{"type": "text", "text": FIRST}]
    elif mode == "ambiguous":
        body.update(
            text="other legacy text", content=[{"type": "text", "text": FIRST}]
        )
    elif mode == "split":
        # No content part contains a full start marker.
        split = FIRST.index("artifact") + 3
        body["content"] = [
            {"type": "text", "text": FIRST[:split]},
            {"type": "text", "text": FIRST[split:]},
        ]
    message = repo.add_message("alice", cid, body)
    if mode in {"ambiguous", "split"}:
        with pytest.raises(ArtifactConflict):
            update_artifact(
                database,
                "alice",
                message["messageId"],
                0,
                "<p>old</p>",
                "<p>new</p>",
                check_grant=lambda: None,
            )
        return
    result = update_artifact(
        database,
        "alice",
        message["messageId"],
        0,
        "<p>old</p>",
        "<p>new</p>",
        check_grant=lambda: None,
    )
    assert result["text"] == FIRST.replace("old", "new")
    run, _ = GenerationStore(database).begin(
        "alice", {"text": "continue", "conversationId": cid}
    )
    with database.read_snapshot() as db:
        assert (
            generation_model_history(db, "alice", run)[0]["content"]
            == result["text"]
        )
