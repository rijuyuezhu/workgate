"""Terminal canonical model steps preserve complete exchanges, not UI snapshots."""

import json

import pytest

from workgate.hosted_chat.database import SQLiteDatabase
from workgate.hosted_chat.generation_actor_lease import ActorLease
from workgate.hosted_chat.generations import GenerationStore
from workgate.hosted_chat.model_history import completed_model_steps


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
