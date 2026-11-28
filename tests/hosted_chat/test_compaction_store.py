"""Compaction stores derived checkpoints, never deletes or overwrites source history."""

import json
from dataclasses import replace

import pytest

from workgate.hosted_chat.compaction_store import (
    commit_compaction,
    plan_compaction,
    project_compaction,
)
from workgate.hosted_chat.database import SQLiteDatabase
from workgate.hosted_chat.generation_actor_lease import (
    ActorLease,
    ActorLeaseLost,
)
from workgate.hosted_chat.generations import (
    GenerationStore,
    generation_text_history,
)
from workgate.hosted_chat.storage import WorkspaceRepository


@pytest.fixture
def branch(tmp_path):
    db = SQLiteDatabase(tmp_path)
    store = GenerationStore(db)
    job = None
    lease = None
    for index in range(4):
        payload = {"text": f"question {index} " + "details " * 150}
        if job is not None:
            payload.update(
                conversationId=job.conversation_id,
                parentMessageId=job.response_message_id,
            )
        job, _ = store.begin("alice", payload)
        lease = ActorLease.claim(db, "alice", job.run_id)
        assert lease is not None
        if index < 3:
            assert store.finish(
                "alice",
                job.run_id,
                status="complete",
                text="answer " * 100,
                actor_lease=lease,
            )
    assert job is not None and lease is not None
    return db, store, job, lease


def history(db, job):
    with db.read_snapshot() as connection:
        return generation_text_history(connection, "alice", job)


def test_checkpoint_preserves_history_and_reuses_exact_branch(branch):
    db, store, job, lease = branch
    original = history(db, job)
    plan = plan_compaction(lease)
    assert plan is not None and len(plan.source) == 4
    record = commit_compaction(
        lease, plan, "Prior discussion.", check_grant=lambda: None
    )
    assert (
        commit_compaction(
            lease, plan, "Prior discussion.", check_grant=lambda: None
        )
        == record
    )
    assert history(db, job) == original
    model_messages = [{"role": "system", "content": "Instructions"}, *original]
    projection = project_compaction(lease, model_messages)
    assert projection[0] == model_messages[0]
    assert "Prior discussion." in projection[1]["content"]
    assert projection[2:] == original[4:]
    assert model_messages[1:] == original
    assert "Prior discussion." not in json.dumps(
        store.events("alice", job.run_id)
    )
    assert (
        len(
            [
                event
                for _, event in store.events("alice", job.run_id)
                if event.get("event") == "on_compaction"
            ]
        )
        == 1
    )
    assert store.finish(
        "alice", job.run_id, status="complete", text="done", actor_lease=lease
    )
    next_job, _ = store.begin(
        "alice",
        {
            "text": "Next question",
            "conversationId": job.conversation_id,
            "parentMessageId": job.response_message_id,
        },
    )
    next_lease = ActorLease.claim(db, "alice", next_job.run_id)
    assert next_lease is not None
    next_history = history(db, next_job)
    assert (
        "Prior discussion."
        in project_compaction(next_lease, next_history)[0]["content"]
    )
    assert store.finish(
        "alice",
        next_job.run_id,
        status="complete",
        text="next",
        actor_lease=next_lease,
    )
    fork, _ = store.begin(
        "alice",
        {
            "text": "Fork",
            "conversationId": job.conversation_id,
            "parentMessageId": "00000000-0000-0000-0000-000000000000",
        },
    )
    fork_lease = ActorLease.claim(db, "alice", fork.run_id)
    assert fork_lease is not None
    fork_history = history(db, fork)
    assert project_compaction(fork_lease, fork_history) is fork_history
    assert plan_compaction(fork_lease) is None


def test_compaction_rejects_stale_source_and_failed_summary(
    branch, monkeypatch
):
    from workgate.hosted_chat import compaction_store

    db, store, job, lease = branch
    plan = plan_compaction(lease)
    assert plan is not None
    with pytest.raises(ValueError, match="retained turn count"):
        plan_compaction(lease, retain_turns=0)
    with pytest.raises(PermissionError):
        plan_compaction(replace(lease, detached_task_id="child"))
    with pytest.raises(PermissionError):
        plan_compaction(replace(lease, wakeup_task_id="wake"))
    for summary in ("", " " * 10, "x" * 65537):
        with pytest.raises(ValueError, match="Invalid compaction"):
            commit_compaction(lease, plan, summary, check_grant=lambda: None)
    with pytest.raises(ValueError, match="does not reduce"):
        commit_compaction(
            lease, plan, "long word " * 1000, check_grant=lambda: None
        )
    repo = WorkspaceRepository(db)
    repo.save_setting(
        "alice",
        "webui.filters.config",
        {"assembled_context": {"pii": {"starterPatterns": ["bearer_header"]}}},
    )
    with pytest.raises(PermissionError, match="source or policy changed"):
        commit_compaction(lease, plan, "safe", check_grant=lambda: None)
    updated = plan_compaction(lease)
    assert updated is not None
    monkeypatch.setattr(compaction_store, "EVENT_LIMIT", 1)
    with pytest.raises(ValueError, match="retention limit"):
        commit_compaction(lease, updated, "safe", check_grant=lambda: None)
    original = history(db, job)
    assert project_compaction(lease, original) is original
    monkeypatch.setattr(compaction_store, "EVENT_LIMIT", 100)
    commit_compaction(lease, updated, "safe", check_grant=lambda: None)
    with pytest.raises(ValueError, match="same or a newer"):
        commit_compaction(lease, updated, "different", check_grant=lambda: None)
    repo.save_setting("alice", "webui.filters.config", {})
    assert project_compaction(lease, original) is original
    lease.release()
    with pytest.raises(ActorLeaseLost):
        plan_compaction(lease)
