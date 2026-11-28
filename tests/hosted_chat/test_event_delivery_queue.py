"""Pinned delivery identity, live source gates, bounded queue and coalescing."""

import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from workgate.hosted_chat import event_delivery_queue as queue
from workgate.hosted_chat.agents import AgentRepository
from workgate.hosted_chat.database import SQLiteDatabase
from workgate.hosted_chat.event_source_keys import EventSourceKeys
from workgate.hosted_chat.generations import GenerationStore
from workgate.hosted_chat.storage import WorkspaceRepository


@pytest.fixture
def state(tmp_path):
    db = SQLiteDatabase(tmp_path)
    repo = AgentRepository(db)
    agent = repo.create(
        "alice", {"name": "Agent", "provider": "openAI", "model": "test"}
    )["id"]
    keys = EventSourceKeys(db)
    source = keys.mint("alice")["id"]
    other = keys.mint("alice", permissions=["fire"])["id"]
    body = {
        "mode": "fire",
        "event": {"id": "event-1", "type": "update", "occurredAt": 123},
        "target": {"agentId": agent},
        "input": "Check progress",
    }
    return db, keys, queue.EventDeliveryQueue(db), source, other, body


def test_concurrent_enqueue_and_conflict(state):
    db, keys, repo, source, other, body = state
    with ThreadPoolExecutor(max_workers=4) as pool:
        rows = list(
            pool.map(
                lambda _: repo.enqueue("alice", source, "once", body), range(4)
            )
        )
    assert sum(not row["replayed"] for row in rows) == 1
    ident = rows[0]["id"]
    assert len({row["id"] for row in rows}) == 1
    assert repo.get("alice", source, ident)["status"] == "pending"
    assert repo.get("alice", other, ident) is None
    with pytest.raises(queue.EventConflict):
        repo.enqueue("alice", source, "once", {**body, "input": "different"})
    with pytest.raises(queue.EventConflict):
        repo.enqueue(
            "alice", source, "once", {**body, "orderingKey": "changed"}
        )
    with pytest.raises(PermissionError):
        repo.get("bob", source, ident)
    assert keys.revoke("alice", source)
    with pytest.raises(PermissionError):
        repo.enqueue("alice", source, "once", body)
    with db.read_snapshot() as conn:
        assert (
            conn.execute(
                "SELECT status FROM agent_event_deliveries WHERE id=?", (ident,)
            ).fetchone()[0]
            == "dead"
        )


@pytest.mark.parametrize(
    "patch",
    [
        {"mode": []},
        {"mode": "unknown"},
        {"event": {}},
        {"event": {"id": "a", "type": "b", "occurredAt": True}},
        {"target": {}},
        {"input": ""},
        {"input": "a" * 131073},
        {"coalesce": {"key": "x"}},
        {"orderingKey": ""},
        {"bindingId": "fake"},
        {"expectedAction": {"toolName": "task"}},
    ],
)
def test_invalid_envelope(state, patch):
    _, _, repo, source, _, body = state
    with pytest.raises(ValueError):
        repo.enqueue("alice", source, "once", {**body, **patch})


def test_source_scope_and_owner_target(state, monkeypatch):
    _, _, repo, source, other, body = state
    with pytest.raises(PermissionError):
        repo.enqueue("bob", source, "once", body)
    with pytest.raises(PermissionError):
        repo.enqueue(
            "alice",
            other,
            "once",
            {**body, "mode": "continue", "bindingId": "fake"},
        )
    with pytest.raises(PermissionError):
        repo.enqueue(
            "alice", source, "once", {**body, "target": {"agentId": "fake"}}
        )
    monkeypatch.setattr(queue, "_MAX_PENDING", 1)
    repo.enqueue("alice", source, "once", body)
    with pytest.raises(queue.EventConflict, match="capacity"):
        repo.enqueue("alice", source, "second", body)
    with pytest.raises(ValueError):
        repo.get("alice", source, "bad")


def test_steer_denies_interactive_native_root(state):
    db, _, repo, source, _, body = state
    generation, _ = GenerationStore(db).begin(
        "alice",
        {"text": "ordinary", "agent_id": body["target"]["agentId"]},
        native_admission={
            "scopes": ["shell:read", "shell:write", "shell:execute"],
            "task_machine": None,
        },
    )
    target = {
        "agentId": body["target"]["agentId"],
        "conversationId": generation.conversation_id,
        "generationCreatedAt": generation.generation_created_at,
    }
    with pytest.raises(PermissionError, match="model-only"):
        repo.enqueue(
            "alice",
            source,
            "steer",
            {**body, "mode": "steer", "target": target},
        )


def test_coalescing_authenticated_binding(state, monkeypatch):
    db, _, repo, source, other, body = state
    agent = body["target"]["agentId"]
    workspace = WorkspaceRepository(db)
    parent = workspace.create_conversation("alice", {"agent_id": agent})[
        "conversationId"
    ]
    child = workspace.create_conversation("alice", {"agent_id": agent})[
        "conversationId"
    ]
    binding = "evtbind_" + "a" * 48
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO agent_event_bindings(id,owner,source_key_id,actor_id,parent_conversation_id,parent_message_id,parent_agent_id,agent_id,conversation_id,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                binding,
                "alice",
                source,
                "actor",
                parent,
                "message",
                agent,
                agent,
                child,
                1,
            ),
        )
    monkeypatch.setattr(queue, "_now", lambda: 1000)
    incoming = {
        **body,
        "mode": "continue",
        "bindingId": binding,
        "coalesce": {"key": "updates"},
        "target": {"agentId": "spoof", "conversationId": "spoof"},
    }
    first = repo.enqueue("alice", source, "one", incoming)
    second = repo.enqueue(
        "alice",
        source,
        "two",
        {
            **incoming,
            "event": {"id": "event-2", "type": "update", "occurredAt": 1},
        },
    )
    assert first["availableAt"] == second["availableAt"] == 1750
    with db.read_snapshot() as conn:
        root = conn.execute(
            "SELECT * FROM agent_event_deliveries WHERE id=?", (first["id"],)
        ).fetchone()
        member = conn.execute(
            "SELECT * FROM agent_event_deliveries WHERE id=?", (second["id"],)
        ).fetchone()
        assert (
            member["batch_root_id"] == root["id"] and root["batch_count"] == 2
        )
        envelope = json.loads(root["envelope"])
        assert envelope["target"]["agentId"] == agent
        assert envelope["target"]["conversationId"] == child
        assert envelope["principal"] == {"userId": "alice"}
        batch = json.loads(queue._batch_input(root, [member]))
        assert [event["deliveryId"] for event in batch["events"]] == [
            "two",
            "one",
        ]
    with pytest.raises(PermissionError):
        repo.enqueue("alice", other, "one", incoming)
    with pytest.raises(PermissionError):
        repo.enqueue(
            "alice",
            source,
            "three",
            {**incoming, "expectedAction": {"toolName": "task"}},
        )


def test_envelope_metadata_and_settled_receipt(state):
    db, _, repo, source, _, body = state
    body = {
        **body,
        "principal": {"userId": "spoof"},
        "event": {
            **body["event"],
            "source": {"id": "spoof"},
            "payload": {"count": 2},
        },
    }
    result = repo.enqueue("alice", source, "with-payload", body)
    with db.connect() as conn:
        row = conn.execute(
            "SELECT envelope FROM agent_event_deliveries WHERE id=?",
            (result["id"],),
        ).fetchone()
        envelope = json.loads(row[0])
        assert envelope["event"]["source"]["id"] == source
        assert envelope["event"]["payload"] == {"count": 2}
        assert envelope["principal"] == {"userId": "alice"}
        conn.execute(
            "UPDATE agent_event_deliveries SET status='dead',error_code='EXAMPLE',error_type='ValueError',result=? WHERE id=?",
            (json.dumps({"ok": False}), result["id"]),
        )
    receipt = repo.get("alice", source, result["id"])
    assert receipt is not None and receipt["error"]["code"] == "EXAMPLE"
    assert receipt["result"] == {"ok": False} and "settledAt" in receipt


def test_invalid_body_budget(state):
    _, _, repo, source, _, body = state
    with pytest.raises(ValueError):
        repo.enqueue("alice", source, "bad key", body)
    with pytest.raises(ValueError):
        repo.enqueue(
            "alice", source, "large", {**body, "padding": "x" * (1024 * 1024)}
        )
    with pytest.raises(ValueError):
        repo.enqueue("alice", source, "json", {**body, "invalid": object()})
