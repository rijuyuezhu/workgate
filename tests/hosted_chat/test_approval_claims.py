"""Owner-fenced review state, durable edge identity and one-shot claim races."""

from concurrent.futures import ThreadPoolExecutor

import pytest

from workgate.hosted_chat.agent_runtime import AgentContext, AgentLoopState
from workgate.hosted_chat.approval_claims import ApprovalClaimStore
from workgate.hosted_chat.database import SQLiteDatabase
from workgate.hosted_chat.generation_actor_lease import (
    ActorLease,
    ActorLeaseLost,
)
from workgate.hosted_chat.generations import GenerationConflict, GenerationStore
from workgate.hosted_chat.loop_checkpoint import OwnedLoopCheckpoint
from workgate.hosted_chat.provider_common import ModelToolCall, ProviderConfig


async def started(tmp_path):
    database = SQLiteDatabase(tmp_path)
    store = GenerationStore(database)
    job, _ = store.begin("alice", {"text": "hello", "model": "m"})
    lease = ActorLease.claim(database, "alice", job.run_id)
    assert lease is not None
    checkpoint = OwnedLoopCheckpoint(lease)
    context = AgentContext(
        ProviderConfig("openai", "m", "https://provider.invalid", "secret"), []
    )
    calls = [
        ModelToolCall("read-1", "read", {"path": "a"}),
        ModelToolCall("write-2", "write_file", {"path": "b"}),
    ]
    state = AgentLoopState(
        context,
        [{"role": "user", "content": "hello"}],
        step=2,
        model_turn=1,
        pending_calls=calls,
        tool_step_started=True,
    )
    await checkpoint.save(state, calls)
    return database, store, job, checkpoint, context, state, calls


@pytest.mark.asyncio
async def test_pause_claim_and_replacement(tmp_path):
    (
        database,
        generations,
        job,
        checkpoint,
        context,
        state,
        calls,
    ) = await started(tmp_path)
    store = ApprovalClaimStore(database)
    assert store.pending("alice", job.run_id) is None
    pending = store.pause(checkpoint, [calls[1]])
    assert store.pending("alice", job.run_id) == pending
    assert store.pending("bob", job.run_id) is None
    assert not checkpoint.lease.is_current()
    assert ActorLease.claim(database, "alice", job.run_id) is None
    current = generations.get("alice", job.run_id)
    assert current is not None and current.status == "requires_action"
    assert generations.events("alice", job.run_id)[-1][1] == {
        "event": "on_pending_action",
        "data": pending,
    }
    assert checkpoint.saver.has_durable_interrupt(
        checkpoint.active, pending["actionId"]
    )
    with pytest.raises(GenerationConflict):
        generations.begin(
            "alice", {"conversationId": job.conversation_id, "text": "other"}
        )
    with pytest.raises(KeyError):
        store.claim("bob", job.run_id, pending["actionId"], [])
    with pytest.raises(ValueError, match="Stale"):
        store.claim("alice", job.run_id, "wrong", [])
    with pytest.raises(ValueError, match="Every paused"):
        store.claim("alice", job.run_id, pending["actionId"], [])
    assert store.pending("alice", job.run_id) == pending
    decisions = [
        {
            "tool_call_id": "write-2",
            "decision": "edit",
            "editedArguments": {"path": "c"},
        }
    ]
    batch = store.claim("alice", job.run_id, pending["actionId"], decisions)
    assert batch.entries[0].tool_call().arguments == {"path": "c"}
    assert store.pending("alice", job.run_id) is None
    with pytest.raises(KeyError):
        store.claim("alice", job.run_id, pending["actionId"], decisions)
    with pytest.raises(ActorLeaseLost):
        store.approved_batch(checkpoint)
    lease = ActorLease.claim(database, "alice", job.run_id)
    assert lease is not None
    resumed = OwnedLoopCheckpoint(lease)
    with pytest.raises(ValueError, match="Restore"):
        store.approved_batch(resumed)
    assert resumed.restore(context) is not None
    assert store.approved_batch(resumed) == batch
    with pytest.raises(ValueError, match="already been claimed"):
        store.pause(resumed, [calls[1]])
    await resumed.save(state, calls)
    assert store.approved_batch(resumed) is None


@pytest.mark.asyncio
async def test_pause_rejects_unsaved_modified_and_settled_calls(tmp_path):
    database, _, _, checkpoint, _, _, calls = await started(tmp_path)
    store = ApprovalClaimStore(database)
    for bad in [
        ModelToolCall("new", "read", {}),
        ModelToolCall("read-1", "read", {"path": "changed"}),
    ]:
        with pytest.raises(ValueError, match="unsettled saved"):
            store.pause(checkpoint, [bad])
    checkpoint.saver.put_writes(
        checkpoint.active, [("tool_receipt", {})], "receipt:read-1"
    )
    with pytest.raises(ValueError, match="unsettled saved"):
        store.pause(checkpoint, [calls[0]])
    assert checkpoint.lease.is_current()


@pytest.mark.asyncio
async def test_claim_race(tmp_path):
    database, _, job, checkpoint, _, _, calls = await started(tmp_path)
    store = ApprovalClaimStore(database)
    pending = store.pause(checkpoint, calls)
    decisions = [
        {"tool_call_id": call.call_id, "decision": "approve"} for call in calls
    ]

    def claim():
        try:
            store.claim("alice", job.run_id, pending["actionId"], decisions)
            return True
        except KeyError:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _: claim(), range(2))) == [False, True]


@pytest.mark.asyncio
async def test_claim_rejects_missing_durable_marker(tmp_path):
    database, _, job, checkpoint, _, _, calls = await started(tmp_path)
    store = ApprovalClaimStore(database)
    pending = store.pause(checkpoint, calls)
    with database.connect() as db:
        db.execute(
            "DELETE FROM agent_checkpoint_writes WHERE channel='__interrupt__'"
        )
    decisions = [
        {"tool_call_id": call.call_id, "decision": "approve"} for call in calls
    ]
    with pytest.raises(ValueError, match="matching durable interruption"):
        store.claim("alice", job.run_id, pending["actionId"], decisions)
    assert store.pending("alice", job.run_id) == pending


@pytest.mark.asyncio
async def test_pause_rollback_and_expired_edge(tmp_path, monkeypatch):
    database, generations, job, checkpoint, _, _, calls = await started(
        tmp_path
    )
    store = ApprovalClaimStore(database)
    # Force failure after the interrupt write: the state and marker must roll back.
    with database.connect() as db:
        db.execute(
            "CREATE TRIGGER reject_pending BEFORE INSERT ON generation_events "
            "WHEN json_extract(NEW.data,'$.event')='on_pending_action' "
            "BEGIN SELECT RAISE(ABORT,'test failure'); END"
        )
    import sqlite3

    with pytest.raises(sqlite3.IntegrityError, match="test failure"):
        store.pause(checkpoint, calls)
    assert checkpoint.lease.is_current()
    saved = checkpoint.saver.get_tuple(checkpoint.active)
    assert saved is not None and not saved["pendingWrites"]
    assert store.pending("alice", job.run_id) is None
    with database.connect() as db:
        db.execute("DROP TRIGGER reject_pending")
    pending = store.pause(checkpoint, calls)
    with database.connect() as db:
        db.execute("UPDATE agent_checkpoints SET expires_at=0")
    decisions = [
        {"tool_call_id": call.call_id, "decision": "approve"} for call in calls
    ]
    with pytest.raises(ValueError, match="missing or stale"):
        store.claim("alice", job.run_id, pending["actionId"], decisions)
    assert store.pending("alice", job.run_id) == pending
