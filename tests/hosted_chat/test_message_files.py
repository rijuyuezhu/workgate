"""Owner-bound message attachment admission without client-supplied evidence."""

import json

import pytest

from workgate.hosted_chat.compaction_store import plan_compaction
from workgate.hosted_chat.database import SQLiteDatabase
from workgate.hosted_chat.generation_actor_lease import ActorLease
from workgate.hosted_chat.generations import (
    GenerationStore,
    generation_model_history,
    generation_text_history,
)
from workgate.hosted_chat.steering import (
    SteeringStore,
    SteerRejection,
    reconcile_applied_steers,
)
from workgate.hosted_chat.storage import WorkspaceRepository


@pytest.fixture
def store(tmp_path):
    database = SQLiteDatabase(tmp_path)
    with database.connect() as db:
        for ident, owner in (
            ("own", "alice"),
            ("other", "bob"),
            ("pending", "alice"),
        ):
            db.execute(
                "INSERT INTO attachments(id,owner,name,path,content_type,metadata) VALUES(?,?,?,?,?,?)",
                (
                    ident,
                    owner,
                    "actual.txt",
                    "/private/path",
                    "text/plain",
                    json.dumps({"extracted_text": "private contents"}),
                ),
            )
        db.execute(
            "INSERT INTO attachment_deletions(file_id,owner,original_path,quarantine_path,phase) VALUES(?,?,?,?,?)",
            ("pending", "alice", "/private/path", "/quarantine", "prepared"),
        )
    return GenerationStore(database)


def test_canonical_user_refs_and_retry(store):
    forged = {
        "file_id": "own",
        "filename": "fake",
        "filepath": "https://foreign",
        "content": "fake bytes",
    }
    payload = {
        "text": "question",
        "files": [forged, forged],
        "clientRequestId": "stable",
    }
    job, fresh = store.begin(
        "alice", payload, native_admission={"scopes": ["shell:read"]}
    )
    assert fresh
    created = store.events("alice", job.run_id)[0][1]["message"]["files"]
    assert created == [
        {
            "file_id": "own",
            "filename": "actual.txt",
            "type": "text/plain",
            "filepath": "/api/ui/chat/files/download/own",
            "source": "local",
        }
    ]
    with store.database.read_snapshot() as db:
        assert generation_model_history(db, "alice", job) == [
            {
                "role": "user",
                "content": "question",
                "files": [{"file_id": "own"}],
            }
        ]
        assert generation_text_history(db, "alice", job) == [
            {"role": "user", "content": "question"}
        ]
    with store.database.connect() as db:
        db.execute("DELETE FROM attachments WHERE id='own'")
    assert store.begin("alice", payload)[1] is False
    with pytest.raises(ValueError, match="different attachments"):
        store.begin("alice", {**payload, "files": []})


@pytest.mark.parametrize(
    "files",
    [
        [{"file_id": "other"}],
        [{"file_id": "pending"}],
        [{"file_id": "missing"}],
        [{}],
        False,
        [{"file_id": "own"}] * 33,
        [{"file_id": "x" * 129}],
    ],
)
def test_invalid_admission_rolls_back(store, files):
    with pytest.raises((ValueError, KeyError)):
        store.begin("alice", {"text": "test", "files": files})
    with store.database.read_snapshot() as db:
        for table in ("conversations", "runs", "messages", "generation_events"):
            assert (
                db.execute("SELECT count(*) FROM " + table).fetchone()[0] == 0
            )


def test_attachment_scope_and_manual(store):
    files = [{"file_id": "own"}]
    with pytest.raises(PermissionError, match="read scope"):
        store.begin(
            "alice",
            {"text": "test", "files": files},
            native_admission={"scopes": ["audit:read"]},
        )
    job, _ = store.begin(
        "alice", {"text": "test"}, native_admission={"scopes": ["audit:read"]}
    )
    steer = SteeringStore(store.database)
    with pytest.raises(PermissionError, match="read scope"):
        steer.enqueue(
            "alice",
            {
                "conversationId": job.conversation_id,
                "text": "follow",
                "files": files,
            },
            preempt_capable=True,
        )
    lease = ActorLease.claim(store.database, "alice", job.run_id)
    assert lease is not None
    store.finish(
        "alice", job.run_id, status="complete", text="done", actor_lease=lease
    )
    lease.release()
    with pytest.raises(ValueError, match="interactive"):
        store.begin(
            "alice",
            {
                "conversationId": job.conversation_id,
                "parentMessageId": job.response_message_id,
                "compact": True,
                "files": files,
            },
        )


def test_steering_identity_replay_and_exact_delivery(store):
    job, _ = store.begin("alice", {"text": "test"})
    steer = SteeringStore(store.database)
    body = {
        "conversationId": job.conversation_id,
        "text": "follow",
        "files": [{"file_id": "own", "filename": "fake"}],
        "clientSteerId": "stable",
    }
    for ident in ("other", "pending", "missing"):
        with pytest.raises(SteerRejection, match="INVALID_FILES"):
            steer.enqueue(
                "alice",
                {**body, "files": [{"file_id": ident}]},
                preempt_capable=True,
            )
    ack = steer.enqueue("alice", body, preempt_capable=True)
    assert (
        steer.pending("alice", job.run_id)[0]["files"][0]["filename"]
        == "actual.txt"
    )
    assert steer.enqueue(
        "alice", {**body, "files": [{"file_id": "own"}]}, preempt_capable=True
    )["replayed"]
    with pytest.raises(SteerRejection, match="STEER_IDEMPOTENCY_CONFLICT"):
        steer.enqueue("alice", {**body, "files": []}, preempt_capable=True)
    lease = ActorLease.claim(store.database, "alice", job.run_id)
    assert lease is not None
    parts = steer.drain(lease, boundary="terminal")
    messages = []
    assert reconcile_applied_steers(messages, parts)
    assert messages[0]["files"] == [{"file_id": "own"}]
    assert messages[0]["steerId"] == ack["steerId"]
    assert not reconcile_applied_steers(messages, parts)


def test_unadmitted_metadata_is_not_provider_evidence(store):
    repo = WorkspaceRepository(store.database)
    cid = repo.create_conversation("alice", {})["conversationId"]
    message = repo.add_message(
        "alice",
        cid,
        {"role": "user", "text": "import", "files": [{"file_id": "own"}]},
    )
    job, _ = store.begin(
        "alice",
        {
            "conversationId": cid,
            "parentMessageId": message["messageId"],
            "text": "next",
        },
    )
    with store.database.read_snapshot() as db:
        assert generation_model_history(db, "alice", job)[0] == {
            "role": "user",
            "content": "import",
        }


def test_attachment_prefix_binds_missing_projection(store):
    job, _ = store.begin(
        "alice", {"text": "long " * 100, "files": [{"file_id": "own"}]}
    )
    lease = ActorLease.claim(store.database, "alice", job.run_id)
    assert lease is not None
    store.finish(
        "alice", job.run_id, status="complete", text="answer", actor_lease=lease
    )
    lease.release()
    next_job, _ = store.begin(
        "alice", {"conversationId": job.conversation_id, "text": "next"}
    )
    next_lease = ActorLease.claim(store.database, "alice", next_job.run_id)
    assert next_lease is not None
    plan = plan_compaction(next_lease, retain_turns=1)
    assert plan is not None and plan.canonical_fingerprint is not None
    assert "cannot be inlined" in str(plan.source)
    assert "private contents" not in str(plan.source)
