"""Canonical provider history is bound to terminal host records and exact ancestry."""

import json

import pytest

from workgate.hosted_chat.compaction_store import (
    commit_compaction,
    plan_compaction,
    project_compaction,
)
from workgate.hosted_chat.database import SQLiteDatabase
from workgate.hosted_chat.generation_actor_lease import ActorLease
from workgate.hosted_chat.generations import (
    GenerationStore,
    generation_model_history,
    generation_text_history,
)


@pytest.fixture
def branch(tmp_path):
    database = SQLiteDatabase(tmp_path)
    store = GenerationStore(database)
    first, _ = store.begin("alice", {"text": "first " + "context " * 100})
    lease = ActorLease.claim(database, "alice", first.run_id)
    assert lease is not None
    steps = [
        {
            "role": "assistant",
            "content": "before",
            "tool_calls": [
                {
                    "id": "call1",
                    "type": "function",
                    "function": {"name": "task", "arguments": "{}"},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call1",
            "content": "exact tool result " * 100,
        },
        {
            "role": "user",
            "source": "steer",
            "steerId": "steer1",
            "content": "follow up",
        },
        {
            "role": "assistant",
            "content": "after",
            "reasoning": [{"type": "thinking", "thinking": "explanation"}],
        },
    ]
    assert store.finish(
        "alice",
        first.run_id,
        status="complete",
        text="beforeafter",
        model_steps=steps,
        actor_lease=lease,
    )
    lease.release()
    second, _ = store.begin(
        "alice",
        {
            "conversationId": first.conversation_id,
            "parentMessageId": first.response_message_id,
            "text": "next",
        },
    )
    return database, store, first, second, steps


def history(database, job, *, owner="alice", ids=False):
    with database.read_snapshot() as db:
        return generation_model_history(db, owner, job, include_message_ids=ids)


def test_exact_canonical_branch_preserves_order_and_text_consumer(branch):
    db, store, first, second, steps = branch
    projected = history(db, second)
    assert projected[1:-1] == steps
    assert projected[-1] == {"role": "user", "content": "next"}
    assert [m["messageId"] for m in history(db, second, ids=True)[1:-1]] == [
        first.response_message_id
    ] * 4
    with db.read_snapshot() as connection:
        text = generation_text_history(connection, "alice", second)
    assert text[1] == {"role": "assistant", "content": "beforeafter"}
    with pytest.raises(PermissionError, match="identity"):
        history(db, second, owner="bob")
    lease = ActorLease.claim(db, "alice", second.run_id)
    assert lease is not None
    assert store.finish(
        "alice",
        second.run_id,
        status="complete",
        text="second reply",
        actor_lease=lease,
    )
    lease.release()
    fork, _ = store.begin(
        "alice",
        {
            "conversationId": first.conversation_id,
            "parentMessageId": first.user_message_id,
            "text": "other branch",
        },
    )
    assert history(db, fork) == [
        projected[0],
        {"role": "user", "content": "other branch"},
    ]


@pytest.mark.parametrize(
    "mode", ["parent", "terminal", "display", "status", "orphan", "malformed"]
)
def test_canonical_corruption_fails_closed(branch, mode):
    database, _, first, second, _ = branch
    with database.connect() as db:
        if mode == "terminal":
            db.execute(
                "DELETE FROM generation_events WHERE run_id=? AND json_extract(data,'$.final')=1",
                (first.run_id,),
            )
        elif mode == "display":
            db.execute(
                "UPDATE messages SET content=? WHERE id=?",
                (
                    json.dumps(
                        [
                            {
                                "type": "tool_call",
                                "tool_call": {"output": "forged"},
                            }
                        ]
                    ),
                    first.response_message_id,
                ),
            )
        elif mode == "status":
            db.execute(
                "UPDATE runs SET status='error' WHERE id=?", (first.run_id,)
            )
        else:
            row = db.execute(
                "SELECT state FROM runs WHERE id=?", (first.run_id,)
            ).fetchone()
            state = json.loads(row[0])
            if mode == "parent":
                state["userMessageId"] = "foreign"
            elif mode == "orphan":
                state["modelSteps"] = [
                    {
                        "role": "tool",
                        "tool_call_id": "missing",
                        "content": "forged",
                    }
                ]
            else:
                state["modelSteps"] = [False]
            db.execute(
                "UPDATE runs SET state=? WHERE id=?",
                (json.dumps(state), first.run_id),
            )
    with pytest.raises(PermissionError):
        history(database, second)


def test_rich_compaction_matches_exact_full_tool_result(branch):
    database, _, first, second, steps = branch
    lease = ActorLease.claim(database, "alice", second.run_id)
    assert lease is not None
    original = history(database, second)
    plan = plan_compaction(lease, retain_turns=1)
    assert plan is not None
    assert plan.source[1:] == steps
    assert "tool_outcome: exact tool result" in plan.semantic_appendix
    commit_compaction(
        lease, plan, "Prior discussion.", check_grant=lambda: None
    )
    projected = project_compaction(lease, original)
    assert projected[0]["role"] == "system"
    assert "Prior discussion." in projected[0]["content"]
    assert projected[1:] == original[-1:]
    with database.connect() as db:
        row = db.execute(
            "SELECT state FROM runs WHERE id=?", (first.run_id,)
        ).fetchone()
        state = json.loads(row[0])
        state["modelSteps"][1]["content"] = "different exact tool result"
        db.execute(
            "UPDATE runs SET state=? WHERE id=?",
            (json.dumps(state), first.run_id),
        )
    changed = history(database, second)
    assert project_compaction(lease, changed) is changed


def test_legacy_ui_tool_preview_never_becomes_model_evidence(branch):
    database, _, first, second, _ = branch
    with database.connect() as db:
        db.execute(
            "UPDATE runs SET state=json_remove(state,'$.modelSteps') WHERE id=?",
            (first.run_id,),
        )
        db.execute(
            "UPDATE messages SET content=? WHERE id=?",
            (
                json.dumps(
                    [
                        {
                            "type": "tool_call",
                            "tool_call": {
                                "id": "fake",
                                "name": "task",
                                "output": "preview",
                            },
                        }
                    ]
                ),
                first.response_message_id,
            ),
        )
    projected = history(database, second)
    assert projected[1] == {"role": "assistant", "content": "beforeafter"}
    assert len(projected) == 3


def test_duplicate_canonical_response_identity_fails_closed(branch):
    database, _, first, second, _ = branch
    with database.connect() as db:
        db.execute(
            "INSERT INTO runs(id,conversation_id,agent_id,status,state,created_at,updated_at) SELECT 'duplicate',conversation_id,agent_id,status,state,created_at,updated_at FROM runs WHERE id=?",
            (first.run_id,),
        )
    with pytest.raises(PermissionError, match="identity changed"):
        history(database, second)
