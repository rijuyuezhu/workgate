"""SQLite run admission and replay, adapted from final upstream source tests."""

import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from workgate.hosted_chat import generations
from workgate.hosted_chat.agents import AgentRepository
from workgate.hosted_chat.credential_cipher import CredentialCipher
from workgate.hosted_chat.database import SQLiteDatabase
from workgate.hosted_chat.generation_actor_lease import (
    ActorLease,
    ActorLeaseLost,
)
from workgate.hosted_chat.generations import GenerationConflict, GenerationStore
from workgate.hosted_chat.provider_profiles import ProviderRepository
from workgate.hosted_chat.storage import WorkspaceRepository


@pytest.fixture
def database(tmp_path):
    return SQLiteDatabase(tmp_path)


def claim(database, job, owner="alice"):
    lease = ActorLease.claim(database, owner, job.run_id)
    assert lease is not None
    return lease


def delta(text, kind="text"):
    return {
        "event": "on_message_delta" if kind == "text" else "on_reasoning_delta",
        "data": {"delta": {"content": {"type": kind, kind: text}}},
    }


def test_generation_transaction_and_event_replay(database):
    store = GenerationStore(database)
    job, fresh = store.begin(
        "alice",
        {
            "conversationId": "new",
            "clientRequestId": "turn-1",
            "text": "你好",
            "endpoint": "agents",
            "model": "test-model",
            "messageId": "user-1",
        },
        protocol_version=1,
    )
    assert fresh and job.status == "running" and job.generation_created_at > 0
    same, fresh = store.begin(
        "alice",
        {"conversationId": "new", "clientRequestId": "turn-1", "text": "你好"},
        protocol_version=2,
    )
    assert not fresh and same == job and same.protocol_version == 1
    repo = WorkspaceRepository(database)
    assert len(repo.messages("alice", job.conversation_id)["messages"]) == 1
    with pytest.raises(GenerationConflict):
        store.begin(
            "alice",
            {"conversationId": job.conversation_id, "text": "duplicate"},
        )
    lease = claim(database, job)
    store.append(
        "alice",
        job.run_id,
        {"event": "on_run_step", "data": {"id": "step-1"}},
        actor_lease=lease,
    )
    store.append("alice", job.run_id, delta("世界"), actor_lease=lease)
    store.append(
        "alice", job.run_id, delta("thought", "think"), actor_lease=lease
    )
    store.append("alice", job.run_id, delta("!"), actor_lease=lease)
    rows = store.events("alice", job.run_id)
    assert len(rows) == 5 and rows[0][1]["created"]
    assert len(store.events("alice", job.run_id, rows[1][0], limit=1)) == 1
    assert store.events("bob", job.run_id) == []
    assert store.finish(
        "alice", job.run_id, status="complete", actor_lease=lease
    )
    assert not store.finish(
        "alice",
        job.run_id,
        status="complete",
        text="duplicate",
        actor_lease=lease,
    )
    latest = store.latest("alice", job.conversation_id)
    assert latest is not None and latest.status == "complete"
    assert store.latest("bob", job.conversation_id) is None
    assert store.get("bob", job.run_id) is None
    history = repo.messages("alice", job.conversation_id)["messages"]
    final = store.events("alice", job.run_id)[-1][1]
    assert final["final"] and final["generationProtocolVersion"] == 1
    assert history[-1]["text"] == "世界!"
    assert history[-1]["content"] == final["responseMessage"]["content"]
    assert [part["type"] for part in history[-1]["content"]] == [
        "text",
        "think",
        "text",
    ]
    assert final["unreadMarker"] == "run:" + job.run_id
    assert not lease.is_current()
    restarted = GenerationStore(SQLiteDatabase(database.root.parent))
    assert restarted.get("alice", job.run_id) == latest


def test_followup_updates_conversation_model_without_recreating_chat(database):
    repo = WorkspaceRepository(database)
    profiles = ProviderRepository(database, CredentialCipher(database))
    profiles.save(
        {
            "name": "模型一",
            "provider": "openai",
            "base_url": "https://example.invalid/v1",
            "models": ["new-model"],
            "api_key": "secret",
        }
    )
    original = repo.create_conversation(
        "alice",
        {
            "endpoint": "openAI",
            "model": "previous-model",
            "title": "Existing chat",
        },
    )
    cid = original["conversationId"]
    store = GenerationStore(database)
    job, fresh = store.begin(
        "alice",
        {
            "conversationId": cid,
            "endpoint": "模型一",
            "text": "Next turn",
            "lsmSampling": {"temperature": 0},
        },
    )
    assert (
        fresh
        and job.conversation_id == cid
        and job.provider == "模型一"
        and job.model == "new-model"
    )
    restored = repo.conversation("alice", cid)
    assert (
        restored is not None
        and restored["model"] == "new-model"
        and restored["endpoint"] == "模型一"
    )
    assert restored["title"] == "Existing chat"
    assert job.sampling == {"temperature": 0}
    lease = claim(database, job)
    assert store.finish(
        "alice", job.run_id, status="complete", text="done", actor_lease=lease
    )
    next_job, _ = store.begin(
        "alice", {"conversationId": cid, "text": "followup"}
    )
    assert next_job.provider == job.provider and next_job.model == job.model
    assert next_job.sampling == job.sampling
    msgs = repo.messages("alice", cid)["messages"]
    assert msgs[-1]["parentMessageId"] == job.response_message_id
    with database.connect() as db:
        assert all(
            "secret" not in row[0]
            for row in db.execute("SELECT state FROM runs")
        )


@pytest.mark.parametrize(
    "payload",
    [
        {"text": ""},
        {"text": "abc", "clientRequestId": "z" * 300},
        {"text": "task", "conversationId": 123},
        {"text": "task", "projectId": []},
        {"text": "task", "projectId": "missing"},
        {"text": "task", "conversationId": "missing"},
        {"text": "task", "provider": "missing"},
        {"text": "task", "model": {}},
        {"text": "task", "parentMessageId": "missing"},
        {"text": "task", "files": [{}]},
        {"text": "task", "compact": True},
        {"text": "task", "messageId": {}},
        {"text": "task", "lsmSampling": {"temperature": float("nan")}},
    ],
)
def test_generation_rejects_invalid_admission_without_partial_writes(
    database, payload
):
    store = GenerationStore(database)
    with pytest.raises((ValueError, KeyError)):
        store.begin("alice", payload)
    with database.connect() as conn:
        for table in (
            "conversations",
            "runs",
            "messages",
            "generation_requests",
            "generation_events",
        ):
            assert (
                conn.execute("SELECT count(*) FROM " + table).fetchone()[0] == 0
            )


def test_internal_namespace_and_owner_fences(database):
    store = GenerationStore(database)
    for key in ("subagent:child", "subagent-wakeup:child", "trigger_key"):
        with pytest.raises(ValueError, match="Internal"):
            store.begin("alice", {"text": "Spoof", "clientRequestId": key})
    project = WorkspaceRepository(database).create_project("alice", "Research")
    agent = AgentRepository(database).create(
        "alice", {"provider": "openai", "model": "agent-model"}
    )
    job, _ = store.begin(
        "alice",
        {
            "text": "hello",
            "agent_id": agent["id"],
            "projectId": project["id"],
            "clientRequestId": "turn",
        },
    )
    assert job.agent_id == agent["id"] and job.model == "agent-model"
    for payload in (
        {"text": "x", "agent_id": agent["id"]},
        {"text": "x", "projectId": project["id"]},
        {"text": "x", "conversationId": job.conversation_id},
        {"text": "x", "parentMessageId": job.user_message_id},
    ):
        with pytest.raises(KeyError):
            store.begin("bob", payload)
    other, _ = store.begin("bob", {"text": "hello", "clientRequestId": "turn"})
    assert other.run_id != job.run_id
    lease = claim(database, job)
    other_lease = claim(database, other, "bob")
    with pytest.raises(ActorLeaseLost):
        store.append("alice", job.run_id, delta("bad"), actor_lease=other_lease)
    with pytest.raises(ActorLeaseLost):
        store.finish(
            "alice", job.run_id, status="complete", actor_lease=other_lease
        )
    assert store.get("alice", job.run_id) == job
    with database.connect() as db:
        db.execute(
            "UPDATE generation_actor_leases SET expires_at=0 WHERE run_id=?",
            (job.run_id,),
        )
    new_lease = claim(database, job)
    with pytest.raises(ActorLeaseLost):
        store.append("alice", job.run_id, delta("stale"), actor_lease=lease)
    with pytest.raises(ActorLeaseLost):
        store.finish("alice", job.run_id, status="error", actor_lease=lease)
    assert store.finish(
        "alice",
        job.run_id,
        status="interrupted",
        error="Stopped",
        actor_lease=new_lease,
    )
    final = store.events("alice", job.run_id)[-1][1]
    assert final["error"] == "Stopped"
    assert final["responseMessage"]["content"][-1] == {
        "type": "error",
        "error": "Stopped",
    }


def test_admission_races_and_event_limits(database, monkeypatch):
    store = GenerationStore(database)

    def submit(i):
        return store.begin(
            "alice",
            {"text": "same", "clientRequestId": "request"},
            protocol_version=1 + i % 2,
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(submit, range(16)))
    assert sum(fresh for _, fresh in results) == 1
    assert len({job.run_id for job, _ in results}) == 1
    job = results[0][0]
    assert all(
        other.protocol_version == job.protocol_version for other, _ in results
    )
    lease = claim(database, job)
    with pytest.raises(ValueError, match="256 KiB"):
        store.append(
            "alice", job.run_id, delta("a" * (257 * 1024)), actor_lease=lease
        )
    with pytest.raises(ValueError, match="Unsupported"):
        store.append(
            "alice",
            job.run_id,
            {"event": "on_run_step_completed", "data": {}},
            actor_lease=lease,
        )
    monkeypatch.setattr(generations, "EVENT_LIMIT", 1)
    with pytest.raises(ValueError, match="retention"):
        store.append("alice", job.run_id, delta("overflow"), actor_lease=lease)
    assert store.finish(
        "alice", job.run_id, status="aborted", actor_lease=lease
    )
    assert store.events("alice", job.run_id)[-1][1]["aborted"]
    with pytest.raises(ActorLeaseLost):
        store.append("alice", job.run_id, delta("late"), actor_lease=lease)
    with database.connect() as db:
        assert (
            db.execute("SELECT count(*) FROM conversations").fetchone()[0] == 1
        )
        assert db.execute("SELECT count(*) FROM messages").fetchone()[0] == 1
        assert (
            json.loads(db.execute("SELECT state FROM runs").fetchone()[0])[
                "generationProtocolVersion"
            ]
            == job.protocol_version
        )


def test_reserved_response_identity_and_failed_write_leave_no_partial_run(
    database,
):
    store = GenerationStore(database)
    job, _ = store.begin("alice", {"text": "first", "messageId": "user-id"})
    for ident in (job.response_message_id, job.user_message_id):
        with pytest.raises(ValueError, match="already in use"):
            store.begin("bob", {"text": "other", "messageId": ident})
    lease = claim(database, job)
    with pytest.raises(ValueError, match="Invalid generation delta"):
        store.append(
            "alice",
            job.run_id,
            {"event": "on_message_delta", "data": {"delta": {}}},
            actor_lease=lease,
        )
    assert len(store.events("alice", job.run_id)) == 1
    with database.connect() as db:
        assert db.execute("SELECT count(*) FROM runs").fetchone()[0] == 1
        assert (
            db.execute("SELECT count(*) FROM conversations").fetchone()[0] == 1
        )
    assert store.finish(
        "alice", job.run_id, status="complete", text="answer", actor_lease=lease
    )
