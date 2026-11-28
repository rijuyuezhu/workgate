"""Source-scoped event generation, ordering, and lost actor fences."""

import pytest

from workgate.hosted_chat.agents import AgentRepository
from workgate.hosted_chat.database import SQLiteDatabase
from workgate.hosted_chat.event_delivery_queue import EventDeliveryQueue
from workgate.hosted_chat.event_generation_queue import EventGenerations
from workgate.hosted_chat.event_source_keys import EventSourceKeys
from workgate.hosted_chat.generation_actor_lease import ActorLease
from workgate.hosted_chat.generations import GenerationStore


@pytest.fixture
def state(tmp_path):
    db = SQLiteDatabase(tmp_path)
    agent = AgentRepository(db).create(
        "alice", {"name": "Events", "provider": "openAI", "model": "test"}
    )["id"]
    keys = EventSourceKeys(db)
    source = keys.mint("alice")["id"]
    queue = EventDeliveryQueue(db)
    body = {
        "mode": "fire",
        "event": {"id": "event", "type": "update", "occurredAt": 123},
        "target": {"agentId": agent},
        "input": "Check progress",
    }
    return db, keys, source, queue, body, EventGenerations(db)


def test_order_and_success(state):
    db, _, source, queue, body, events = state
    first = queue.enqueue("alice", source, "first", body)["id"]
    second = queue.enqueue("alice", source, "second", body)["id"]
    (admitted,) = events.admit()
    assert admitted.delivery_id == first
    assert events.admit() == [admitted]
    assert ActorLease.claim(db, "alice", admitted.run_id) is None
    assert (
        ActorLease.claim(db, "bob", admitted.run_id, event_delivery_id=first)
        is None
    )
    assert (
        ActorLease.claim(db, "alice", admitted.run_id, event_delivery_id=second)
        is None
    )
    lease = ActorLease.claim(
        db, "alice", admitted.run_id, event_delivery_id=first
    )
    assert lease is not None
    assert lease.is_current() and lease.renew()
    assert not events.admit()
    assert (
        ActorLease.claim(db, "alice", admitted.run_id, event_delivery_id=first)
        is None
    )
    GenerationStore(db).finish(
        "alice",
        admitted.run_id,
        status="complete",
        text="Done",
        actor_lease=lease,
    )
    lease.release()
    assert events.refresh() == 1
    assert events.refresh() == 0
    receipt = queue.get("alice", source, first)
    assert receipt is not None and receipt["status"] == "succeeded"
    assert receipt["result"]["runId"] == admitted.run_id
    assert events.admit()[0].delivery_id == second


@pytest.mark.parametrize("revoke", [False, True])
def test_no_replay_after_actor_loss(state, revoke):
    db, keys, source, queue, body, events = state
    delivery = queue.enqueue("alice", source, "once", body)["id"]
    (admitted,) = events.admit()
    lease = ActorLease.claim(
        db, "alice", admitted.run_id, event_delivery_id=delivery
    )
    assert lease is not None
    if revoke:
        keys.revoke("alice", source)
        assert not lease.is_current()
        assert not lease.renew()
    lease.release()
    assert events.refresh() == 1
    assert events.refresh() == 0
    assert not events.admit()
    assert (
        ActorLease.claim(
            db, "alice", admitted.run_id, event_delivery_id=delivery
        )
        is None
    )
    with db.read_snapshot() as conn:
        assert (
            conn.execute(
                "SELECT status FROM runs WHERE id=?", (admitted.run_id,)
            ).fetchone()[0]
            == "interrupted"
        )
        assert (
            conn.execute(
                "SELECT status FROM agent_event_deliveries WHERE id=?",
                (delivery,),
            ).fetchone()[0]
            == "dead"
        )


def test_expiry_before_admission(state):
    db, _, source, queue, body, events = state
    delivery = queue.enqueue("alice", source, "once", body)["id"]
    with db.connect() as conn:
        conn.execute(
            "UPDATE agent_event_source_keys SET expires_at=1 WHERE id=?",
            (source,),
        )
    assert not events.admit()
    with db.read_snapshot() as conn:
        assert (
            conn.execute(
                "SELECT status FROM agent_event_deliveries WHERE id=?",
                (delivery,),
            ).fetchone()[0]
            == "dead"
        )


def test_bound_batch(state):
    from workgate.hosted_chat.event_bindings import EventBindings

    db, _, source, queue, body, events = state
    agent = body["target"]["agentId"]
    AgentRepository(db).update(
        "alice", agent, {"subagents": {"enabled": True, "allowSelf": True}}
    )
    store = GenerationStore(db)
    parent, _ = store.begin("alice", {"text": "parent", "agent_id": agent})
    lease = ActorLease.claim(db, "alice", parent.run_id)
    assert lease is not None
    store.finish(
        "alice",
        parent.run_id,
        status="complete",
        text="reply",
        actor_lease=lease,
    )
    lease.release()
    binding = EventBindings(db).register(
        "alice",
        source,
        "bind",
        actor_id="actor",
        parent_conversation_id=parent.conversation_id,
        parent_message_id=parent.response_message_id,
        target_agent_id=agent,
    )
    incoming = {
        **body,
        "mode": "continue",
        "bindingId": binding["id"],
        "coalesce": {"key": "batch"},
    }
    root = queue.enqueue("alice", source, "one", incoming)["id"]
    member = queue.enqueue("alice", source, "two", incoming)["id"]
    with db.connect() as conn:
        conn.execute(
            "UPDATE agent_event_deliveries SET available_at=0 WHERE id=?",
            (root,),
        )
    (item,) = events.admit()
    assert item.delivery_id == root
    generation = store.get("alice", item.run_id)
    assert generation is not None
    assert generation.conversation_id == binding["threadId"]
    lease = ActorLease.claim(db, "alice", item.run_id, event_delivery_id=root)
    assert lease is not None and lease.is_current()
    store.finish(
        "alice", item.run_id, status="complete", text="done", actor_lease=lease
    )
    lease.release()
    assert events.refresh() == 1
    assert queue.get("alice", source, member)["status"] == "succeeded"


def test_failed_generation_admission(state):
    db, _, source, queue, body, events = state
    delivery = queue.enqueue("alice", source, "once", body)["id"]
    with db.connect() as conn:
        conn.execute(
            "UPDATE agent_event_deliveries SET envelope=json_set(envelope,'$.input','') WHERE id=?",
            (delivery,),
        )
    assert not events.admit()
    assert queue.get("alice", source, delivery)["status"] == "dead"


@pytest.mark.parametrize("corrupt", ["principal", "agent", "run"])
def test_identity_fails_closed(state, corrupt):
    db, _, source, queue, body, events = state
    delivery = queue.enqueue("alice", source, "once", body)["id"]
    (item,) = events.admit()
    with db.connect() as conn:
        if corrupt == "principal":
            conn.execute(
                "UPDATE agent_event_deliveries SET envelope=json_set(envelope,'$.principal.userId','bob') WHERE id=?",
                (delivery,),
            )
        elif corrupt == "agent":
            conn.execute(
                "UPDATE agent_event_deliveries SET envelope=json_set(envelope,'$.target.agentId','missing') WHERE id=?",
                (delivery,),
            )
        else:
            conn.execute(
                "UPDATE runs SET state=json_set(state,'$.nativeAdmission',json('{}')) WHERE id=?",
                (item.run_id,),
            )
    assert (
        ActorLease.claim(db, "alice", item.run_id, event_delivery_id=delivery)
        is None
    )
    assert events.refresh() == 1
    assert not events.admit()
