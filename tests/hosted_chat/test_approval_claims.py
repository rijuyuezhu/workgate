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


async def started(tmp_path, calls=None):
    database = SQLiteDatabase(tmp_path)
    store = GenerationStore(database)
    job, _ = store.begin("alice", {"text": "hello", "model": "m"})
    lease = ActorLease.claim(database, "alice", job.run_id)
    assert lease is not None
    checkpoint = OwnedLoopCheckpoint(lease)
    context = AgentContext(
        ProviderConfig("openai", "m", "https://provider.invalid", "secret"), []
    )
    calls = (
        calls
        if calls is not None
        else [
            ModelToolCall("read-1", "read", {"path": "a"}),
            ModelToolCall("write-2", "write_file", {"path": "b"}),
        ]
    )
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


@pytest.mark.asyncio
async def test_abort_pending_reuses_terminal_event_and_preserves_streamed_text(
    tmp_path,
):
    import json

    from workgate.hosted_chat.durable_tasks import DurableTaskRepository

    database, generations, job, checkpoint, _, state, calls = await started(
        tmp_path
    )
    store = ApprovalClaimStore(database)
    lease = checkpoint.lease
    cursor = generations.append(
        lease.owner,
        job.run_id,
        {
            "event": "on_message_delta",
            "data": {
                "delta": {
                    "content": {"type": "text", "text": "visible before review"}
                }
            },
        },
        actor_lease=lease,
    )
    await checkpoint.save(
        state,
        calls,
        partial_text="visible before review",
        public_delta_cursor=cursor,
    )
    pending = store.pause(checkpoint, calls)
    action = pending["actionId"]
    assert not store.abort_pending("bob", job.run_id, action)
    assert not store.abort_pending(lease.owner, job.run_id, "stale")
    assert not store.expire_pending(lease.owner, job.run_id, action)
    assert store.abort_pending(lease.owner, job.run_id, action)
    assert not store.abort_pending(lease.owner, job.run_id, action)
    assert not store.expire_pending(lease.owner, job.run_id, action)
    assert DurableTaskRepository(database).is_run_cancelled(job.run_id)
    assert store.pending(lease.owner, job.run_id) is None
    assert ActorLease.claim(database, lease.owner, job.run_id) is None
    with pytest.raises(KeyError):
        store.claim(lease.owner, job.run_id, action, [])
    finished = generations.get(lease.owner, job.run_id)
    assert finished is not None and finished.status == "aborted"
    final = generations.events(lease.owner, job.run_id)[-1][1]
    assert final["final"] and final["aborted"]
    assert final["responseMessage"]["text"] == "visible before review"
    with database.connect() as db:
        saved = json.loads(
            db.execute(
                "SELECT state FROM runs WHERE id=?", (job.run_id,)
            ).fetchone()[0]
        )
    assert "pendingAction" not in saved and "approvalCheckpointId" not in saved
    followup, created = generations.begin(
        lease.owner, {"conversationId": job.conversation_id, "text": "next"}
    )
    assert created and followup.run_id != job.run_id


@pytest.mark.parametrize(
    "damage", ["expiry", "marker", "checkpoint", "deadline"]
)
@pytest.mark.asyncio
async def test_expire_pending_only_unavailable_review(tmp_path, damage):
    from workgate.hosted_chat.durable_tasks import DurableTaskRepository

    database, generations, job, checkpoint, _, _, calls = await started(
        tmp_path
    )
    store = ApprovalClaimStore(database)
    pending = store.pause(checkpoint, calls)
    action = pending["actionId"]
    assert not store.expire_pending("bob", job.run_id, action)
    assert not store.expire_pending("alice", job.run_id, "wrong")
    assert not store.expire_pending("alice", job.run_id, action)
    with database.connect() as db:
        if damage == "expiry":
            db.execute("UPDATE agent_checkpoints SET expires_at=0")
        elif damage == "marker":
            db.execute(
                "DELETE FROM agent_checkpoint_writes WHERE channel='__interrupt__'"
            )
        elif damage == "checkpoint":
            db.execute("DELETE FROM agent_checkpoints")
        else:
            db.execute(
                "UPDATE runs SET state=json_set(state,'$.pendingAction.expiresAt',1) WHERE id=?",
                (job.run_id,),
            )
    if damage == "deadline":
        with pytest.raises(ValueError, match="expired"):
            store.claim("alice", job.run_id, action, [])
    assert store.expire_pending("alice", job.run_id, action)
    current = generations.get("alice", job.run_id)
    assert current is not None and current.status == "interrupted"
    final = generations.events("alice", job.run_id)[-1][1]
    assert final["final"] and "checkpoint" in final["error"]
    assert not DurableTaskRepository(database).is_run_cancelled(job.run_id)
    assert store.pending("alice", job.run_id) is None
    assert not store.expire_pending("alice", job.run_id, action)


@pytest.mark.asyncio
async def test_abort_pending_and_claim_compete_on_same_transition(tmp_path):
    database, generations, job, checkpoint, _, _, calls = await started(
        tmp_path
    )
    store = ApprovalClaimStore(database)
    pending = store.pause(checkpoint, calls)
    action = pending["actionId"]
    decisions = [
        {"tool_call_id": call.call_id, "decision": "approve"} for call in calls
    ]

    def claim():
        try:
            store.claim("alice", job.run_id, action, decisions)
            return True
        except KeyError:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        abort = pool.submit(store.abort_pending, "alice", job.run_id, action)
        review = pool.submit(claim)
        assert abort.result() != review.result()
        won_abort = abort.result()
    current = generations.get("alice", job.run_id)
    assert current is not None
    assert current.status == ("aborted" if won_abort else "running")
    assert not store.abort_pending("alice", job.run_id, action)


@pytest.mark.asyncio
async def test_batched_question_one_shot_answer_and_actor_replacement(tmp_path):
    proposal = ModelToolCall(
        "question",
        "ask_user_question",
        {
            "questions": [
                {"id": "file", "question": "File?"},
                {"id": "format", "question": "Format?"},
            ]
        },
    )
    (
        database,
        generations,
        job,
        checkpoint,
        context,
        state,
        calls,
    ) = await started(tmp_path, [proposal])
    store = ApprovalClaimStore(database)
    pending = store.pause_question(checkpoint, proposal)
    action = pending["actionId"]
    assert pending["payload"]["type"] == "ask_user_question"
    assert store.pending("bob", job.run_id) is None
    assert not checkpoint.lease.is_current()
    with pytest.raises(KeyError):
        store.claim_question("bob", job.run_id, action, {})
    with pytest.raises(ValueError, match="Stale"):
        store.claim_question("alice", job.run_id, "stale", {})
    with pytest.raises(ValueError, match="every question"):
        store.claim_question(
            "alice", job.run_id, action, {"answers": {"file": "a"}}
        )
    with pytest.raises(ValueError, match="Expected tool approval"):
        store.claim("alice", job.run_id, action, [])
    assert store.pending("alice", job.run_id) == pending
    answer = store.claim_question(
        "alice",
        job.run_id,
        action,
        {"answers": {"format": "json", "file": "a"}},
    )
    assert list(answer["answers"]) == ["file", "format"]
    with pytest.raises(KeyError):
        store.claim_question("alice", job.run_id, action, {})
    fresh = ActorLease.claim(database, "alice", job.run_id)
    assert fresh is not None
    resumed = OwnedLoopCheckpoint(fresh)
    with pytest.raises(ValueError, match="Restore"):
        store.answered_question(resumed)
    assert resumed.restore(context) is not None
    assert store.answered_question(resumed) == answer
    with pytest.raises(ValueError, match="already been answered"):
        store.pause_question(resumed, proposal)
    with pytest.raises(ValueError, match="already been answered"):
        store.pause(resumed, calls)
    await resumed.save(state, calls)
    assert store.answered_question(resumed) is None


@pytest.mark.parametrize("decision", ["approve", "edit", "reject"])
@pytest.mark.asyncio
async def test_reviewed_question_retains_exact_approval_across_second_pause(
    tmp_path, decision
):
    proposal = ModelToolCall(
        "question", "ask_user_question", {"question": "Original?"}
    )
    database, _, job, checkpoint, context, _, calls = await started(
        tmp_path, [proposal]
    )
    store = ApprovalClaimStore(database)
    review = store.pause(checkpoint, calls)
    with pytest.raises(ValueError, match="Expected Ask User"):
        store.claim_question(
            "alice", job.run_id, review["actionId"], {"answer": "a"}
        )
    resolution = {"tool_call_id": "question", "decision": decision}
    if decision == "edit":
        resolution["editedArguments"] = {"question": "Edited?"}
    batch = store.claim("alice", job.run_id, review["actionId"], [resolution])
    fresh = ActorLease.claim(database, "alice", job.run_id)
    assert fresh is not None
    resumed = OwnedLoopCheckpoint(fresh)
    assert resumed.restore(context) is not None
    if decision == "reject":
        with pytest.raises(ValueError, match="did not authorize"):
            store.pause_question(resumed, proposal)
        assert fresh.is_current()
        return
    pending = store.pause_question(resumed, proposal)
    assert pending["payload"]["question"]["question"] == (
        "Edited?" if decision == "edit" else "Original?"
    )
    assert pending["actionId"] != review["actionId"]
    with pytest.raises(ValueError, match="Stale"):
        store.claim_question(
            "alice", job.run_id, review["actionId"], {"answer": "a"}
        )
    answer = store.claim_question(
        "alice", job.run_id, pending["actionId"], {"answer": "yes"}
    )
    last = ActorLease.claim(database, "alice", job.run_id)
    assert last is not None
    checkpoint = OwnedLoopCheckpoint(last)
    assert checkpoint.restore(context) is not None
    assert store.approved_batch(checkpoint) == batch
    assert store.answered_question(checkpoint) == answer == {"answer": "yes"}


@pytest.mark.asyncio
async def test_question_requires_lone_exact_proposal_and_expires_without_dispatch(
    tmp_path,
):
    proposal = ModelToolCall(
        "question", "ask_user_question", {"question": "Choose?"}
    )
    database, generations, job, checkpoint, _, state, _ = await started(
        tmp_path, [proposal, ModelToolCall("read", "read", {})]
    )
    store = ApprovalClaimStore(database)
    with pytest.raises(ValueError, match="one exact saved"):
        store.pause_question(checkpoint, proposal)
    state.pending_calls = [proposal]
    await checkpoint.save(state, [proposal])
    with pytest.raises(ValueError, match="unsettled saved"):
        store.pause_question(
            checkpoint,
            ModelToolCall(
                "question", "ask_user_question", {"question": "Changed?"}
            ),
        )
    pending = store.pause_question(checkpoint, proposal)
    assert not store.expire_pending("alice", job.run_id, pending["actionId"])
    with database.connect() as db:
        db.execute("DELETE FROM agent_checkpoints")
    assert store.expire_pending("alice", job.run_id, pending["actionId"])
    current = generations.get("alice", job.run_id)
    assert current is not None and current.status == "interrupted"
