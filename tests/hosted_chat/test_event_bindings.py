"""Direct-child event binding is atomically guarded and source-fenced."""

from concurrent.futures import ThreadPoolExecutor

import pytest

from workgate.hosted_chat.agents import AgentRepository
from workgate.hosted_chat.database import SQLiteDatabase
from workgate.hosted_chat.event_bindings import EventBindings, live_binding
from workgate.hosted_chat.event_envelope import EventConflict
from workgate.hosted_chat.event_source_keys import EventSourceKeys
from workgate.hosted_chat.generation_actor_lease import ActorLease
from workgate.hosted_chat.generations import GenerationStore
from workgate.hosted_chat.storage import WorkspaceRepository


@pytest.fixture
def state(tmp_path):
    db = SQLiteDatabase(tmp_path)
    agents = AgentRepository(db)
    child = agents.create(
        "alice", {"name": "Child", "provider": "openAI", "model": "test"}
    )["id"]
    parent = agents.create(
        "alice",
        {
            "name": "Parent",
            "provider": "openAI",
            "model": "test",
            "subagents": {
                "enabled": True,
                "allowSelf": False,
                "agent_ids": [child],
            },
        },
    )["id"]
    store = GenerationStore(db)
    run, _ = store.begin("alice", {"text": "Question", "agent_id": parent})
    lease = ActorLease.claim(db, "alice", run.run_id)
    assert lease is not None
    store.finish(
        "alice", run.run_id, status="complete", text="Reply", actor_lease=lease
    )
    lease.release()
    keys = EventSourceKeys(db)
    source = keys.mint("alice")["id"]
    return db, keys, source, EventBindings(db), run, parent, child


def test_binding_concurrency_and_guard(state):
    db, keys, source, repo, run, _, child = state
    args = {
        "actor_id": "actor",
        "parent_conversation_id": run.conversation_id,
        "parent_message_id": run.response_message_id,
        "target_agent_id": child,
    }
    with ThreadPoolExecutor(max_workers=4) as pool:
        replies = list(
            pool.map(
                lambda _: repo.register("alice", source, "once", **args),
                range(4),
            )
        )
    assert all(item == replies[0] for item in replies)
    item = replies[0]
    thread = item["threadId"]
    with db.read_snapshot() as conn:
        assert live_binding(conn, "alice", source, item["id"]) is not None
        assert live_binding(conn, "bob", source, item["id"]) is None
        with pytest.raises(ValueError):
            live_binding(conn, "alice", source, "invalid")
    with pytest.raises(EventConflict):
        repo.register(
            "alice", source, "once", **{**args, "actor_id": "different"}
        )
    workspace = WorkspaceRepository(db)
    with pytest.raises(PermissionError):
        GenerationStore(db).begin(
            "alice", {"conversationId": thread, "text": "forge"}
        )
    with pytest.raises(PermissionError):
        workspace.delete_conversation("alice", thread)
    with pytest.raises(PermissionError, match="event bindings"):
        workspace.delete_conversation("alice", run.conversation_id)
    assert keys.revoke("alice", source)
    with db.read_snapshot() as conn:
        assert live_binding(conn, "alice", source, item["id"]) is None
    assert workspace.delete_conversation("alice", thread)
    assert workspace.delete_conversation("alice", run.conversation_id)


def test_binding_revalidates_live_direct_child(state):
    db, _, source, repo, run, parent, child = state
    args = {
        "actor_id": "actor",
        "parent_conversation_id": run.conversation_id,
        "parent_message_id": run.response_message_id,
        "target_agent_id": child,
    }
    item = repo.register("alice", source, "once", **args)
    AgentRepository(db).update(
        "alice", parent, {"subagents": {"enabled": False}}
    )
    with db.read_snapshot() as conn:
        assert live_binding(conn, "alice", source, item["id"]) is None
    with pytest.raises(PermissionError):
        repo.register("alice", source, "once", **args)


@pytest.mark.parametrize(
    "mode",
    [
        "other_owner",
        "bad_key",
        "parent_missing",
        "user_message",
        "not_child",
        "fire_only",
        "bad_actor",
    ],
)
def test_binding_invalid_parent_or_source(state, mode):
    db, keys, source, repo, run, parent, child = state
    args = {
        "actor_id": "actor",
        "parent_conversation_id": run.conversation_id,
        "parent_message_id": run.response_message_id,
        "target_agent_id": child,
    }
    owner = "alice"
    key = "once"
    if mode == "other_owner":
        owner = "bob"
    elif mode == "bad_key":
        key = "bad key"
    elif mode == "parent_missing":
        args["parent_conversation_id"] = "missing"
    elif mode == "user_message":
        args["parent_message_id"] = run.user_message_id
    elif mode == "not_child":
        args["target_agent_id"] = parent
    elif mode == "fire_only":
        source = keys.mint("alice", permissions=["fire"])["id"]
    else:
        args["actor_id"] = ""
    with pytest.raises((ValueError, PermissionError, KeyError)):
        repo.register(owner, source, key, **args)
    with db.read_snapshot() as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM agent_event_bindings"
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute(
                "SELECT count(*) FROM active_subagent_threads"
            ).fetchone()[0]
            == 0
        )
