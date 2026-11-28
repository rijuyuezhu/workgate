"""Terminal canonical model steps preserve complete exchanges, not UI snapshots."""

import json

import pytest

from workgate.hosted_chat.agent_runtime import (
    AgentContext,
    AgentLoopState,
    RuntimeSafetyError,
)
from workgate.hosted_chat.database import SQLiteDatabase
from workgate.hosted_chat.generation_actor_lease import ActorLease
from workgate.hosted_chat.generations import (
    GenerationStore,
    generation_model_history,
)
from workgate.hosted_chat.loop_checkpoint import OwnedLoopCheckpoint
from workgate.hosted_chat.model_history import completed_model_steps
from workgate.hosted_chat.provider_common import ModelToolCall, ProviderConfig


def call(ident="a"):
    return {
        "role": "assistant",
        "content": "before",
        "tool_calls": [
            {
                "id": ident,
                "type": "function",
                "function": {"name": "task", "arguments": "{}"},
            }
        ],
    }


def result(ident="a"):
    return {"role": "tool", "content": "full result", "tool_call_id": ident}


def test_canonical_suffix_is_ordered_and_detached():
    prefix = [
        {"role": "system", "content": "instructions"},
        {"role": "user", "content": "question"},
    ]
    suffix = [
        call(),
        result(),
        {
            "role": "user",
            "source": "steer",
            "steerId": "steer1",
            "content": "next",
        },
        {"role": "assistant", "content": "answer"},
    ]
    steps = completed_model_steps(prefix, prefix + suffix)
    assert steps == suffix
    steps[0]["tool_calls"][0]["id"] = "changed"
    assert suffix[0]["tool_calls"][0]["id"] == "a"


@pytest.mark.parametrize(
    "suffix",
    [
        [call()],
        [result()],
        [call(), result("other")],
        [call(), {"role": "assistant", "content": "missing result"}],
        [{"role": "system", "content": "unexpected"}],
        [{"role": "user", "content": "not an admitted steer"}],
        [{"role": "assistant", "tool_calls": None}],
        [{"role": "assistant", "tool_calls": [{"id": "a"}, {"id": "a"}]}],
    ],
)
def test_incomplete_or_unexpected_model_steps_fail_closed(suffix):
    with pytest.raises(ValueError):
        completed_model_steps([], suffix)


def test_prefix_mismatch_is_not_a_new_model_turn():
    with pytest.raises(ValueError, match="prefix changed"):
        completed_model_steps(
            [{"role": "user", "content": "a"}],
            [{"role": "user", "content": "b"}],
        )


def test_terminal_private_model_steps_are_atomic(tmp_path):
    db = SQLiteDatabase(tmp_path)
    store = GenerationStore(db)
    generation, _ = store.begin("alice", {"text": "question"})
    lease = ActorLease.claim(db, "alice", generation.run_id)
    assert lease is not None
    steps = [call(), result()]
    with pytest.raises(ValueError, match="completed model turns"):
        store.finish(
            "alice",
            generation.run_id,
            status="error",
            model_steps=steps,
            actor_lease=lease,
        )
    with db.read_snapshot() as connection:
        row = connection.execute(
            "SELECT state,status FROM runs WHERE id=?", (generation.run_id,)
        ).fetchone()
        assert row["status"] == "running"
        assert "modelSteps" not in json.loads(row["state"])
        assert not connection.execute(
            "SELECT 1 FROM messages WHERE id=?",
            (generation.response_message_id,),
        ).fetchone()
    assert store.finish(
        "alice",
        generation.run_id,
        status="complete",
        model_steps=steps,
        actor_lease=lease,
    )
    with db.read_snapshot() as connection:
        row = connection.execute(
            "SELECT state,status FROM runs WHERE id=?", (generation.run_id,)
        ).fetchone()
        assert row["status"] == "complete"
        assert json.loads(row["state"])["modelSteps"] == steps
        assert connection.execute(
            "SELECT 1 FROM messages WHERE id=?",
            (generation.response_message_id,),
        ).fetchone()
    assert "full result" not in repr(store.events("alice", generation.run_id))


@pytest.mark.parametrize(
    "tail",
    [
        [call("b")],
        [call("b"), result("wrong")],
        [result("orphan")],
        [{"role": "user", "content": "synthetic"}],
        [{"role": "assistant", "tool_calls": [{"id": "a"}, {"id": "a"}]}],
    ],
)
def test_interrupted_suffix_only_retains_complete_prefix(tail):
    prefix = [{"role": "user", "content": "prompt"}]
    complete = [call(), result()]
    assert (
        completed_model_steps(
            prefix, prefix + complete + tail, interrupted=True
        )
        == complete
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["error", "interrupted", "aborted"])
@pytest.mark.parametrize(
    "damage",
    [None, "prefix", "expired", "foreign", "private", "legacy", "invalid"],
)
async def test_interrupted_terminal_checkpoint_recovery(
    tmp_path, status, damage
):
    from workgate.hosted_chat.approval_claims import ApprovalClaimStore

    db = SQLiteDatabase(tmp_path)
    store = GenerationStore(db)
    generation, _ = store.begin(
        "alice", {"text": "question"}, native_admission={}
    )
    lease = ActorLease.claim(db, "alice", generation.run_id)
    assert lease is not None
    with db.read_snapshot() as conn:
        prefix = generation_model_history(conn, "alice", generation)
    checkpoint = OwnedLoopCheckpoint(lease)
    checkpoint.bind_public_history(prefix)
    context = AgentContext(
        ProviderConfig("openai", "m", "https://provider.invalid", "secret"), []
    )
    complete = [call(), result()]
    pending = ModelToolCall("b", "task", {})
    state = AgentLoopState(
        context,
        [*prefix, *complete, call("b")],
        step=3,
        model_turn=2,
        pending_calls=[pending],
    )
    await checkpoint.save(state, [pending])
    with db.connect() as conn:
        if damage == "prefix":
            conn.execute(
                "UPDATE runs SET state=json_set(state,'$.modelHistoryBoundary.sha256','changed') WHERE id=?",
                (lease.run_id,),
            )
        elif damage == "expired":
            conn.execute("UPDATE agent_checkpoints SET expires_at=0")
        elif damage == "foreign":
            conn.execute("UPDATE agent_checkpoints SET owner='bob'")
        elif damage == "private":
            conn.execute(
                "UPDATE agent_checkpoints SET checkpoint_ns=checkpoint_ns||':child:private'"
            )
        elif damage == "legacy":
            conn.execute(
                "UPDATE runs SET state=json_remove(state,'$.modelHistoryBoundary') WHERE id=?",
                (lease.run_id,),
            )
        elif damage == "invalid":
            conn.execute(
                "UPDATE agent_checkpoints SET checkpoint=json_set(checkpoint,'$.channel_values.messages',json('[false]'))"
            )
    if status == "aborted":
        assert ApprovalClaimStore(db).abort_root("alice", lease.run_id)
    else:
        assert store.finish(
            "alice", lease.run_id, status=status, actor_lease=lease
        )
    with db.read_snapshot() as conn:
        saved = json.loads(
            conn.execute(
                "SELECT state FROM runs WHERE id=?", (lease.run_id,)
            ).fetchone()[0]
        )
    if damage is None:
        assert saved["modelSteps"] == complete
        assert saved["modelStepsStatus"] == status
        successor, _ = store.begin(
            "alice",
            {"text": "next", "conversationId": generation.conversation_id},
        )
        with db.read_snapshot() as conn:
            assert generation_model_history(conn, "alice", successor) == [
                *prefix,
                *complete,
                {"role": "user", "content": "next"},
            ]
        assert "full result" not in repr(store.events("alice", lease.run_id))
    else:
        assert "modelSteps" not in saved


def test_root_boundary_is_immutable(tmp_path):
    db = SQLiteDatabase(tmp_path)
    generation, _ = GenerationStore(db).begin("alice", {"text": "question"})
    lease = ActorLease.claim(db, "alice", generation.run_id)
    assert lease is not None
    checkpoint = OwnedLoopCheckpoint(lease)
    prefix = [{"role": "user", "content": "question"}]
    checkpoint.bind_public_history(prefix)
    checkpoint.bind_public_history(prefix)
    with pytest.raises(RuntimeSafetyError, match="prefix changed"):
        checkpoint.bind_public_history([{"role": "user", "content": "other"}])
