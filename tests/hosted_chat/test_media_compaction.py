"""Summary provenance must bind current attachment availability and exact bytes."""

import hashlib
import json

import pytest
from fastapi.testclient import TestClient

from tests.support.hosted_chat_http import provider, runtime_for
from workgate.control.http import hosted_chat
from workgate.control.http.app import build_http_app
from workgate.hosted_chat.compaction_store import (
    commit_compaction,
    plan_compaction,
    project_compaction,
)
from workgate.hosted_chat.credential_cipher import CredentialCipher
from workgate.hosted_chat.database import SQLiteDatabase
from workgate.hosted_chat.generation_actor_lease import ActorLease
from workgate.hosted_chat.generations import (
    GenerationStore,
    generation_model_history,
)
from workgate.hosted_chat.provider_common import ModelFinish
from workgate.hosted_chat.provider_profiles import ProviderRepository
from workgate.hosted_chat.storage import WorkspaceRepository


@pytest.fixture
def branch(tmp_path):
    db = SQLiteDatabase(tmp_path)
    file = db.root.resolve() / "attachments" / "own"
    file.parent.mkdir(parents=True)
    data = b"verified-evidence " * 100
    file.write_bytes(data)
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO attachments(id,owner,name,path,content_type,metadata) VALUES(?,?,?,?,?,?)",
            (
                "own",
                "alice",
                "report.txt",
                str(file),
                "text/plain",
                json.dumps(
                    {
                        "bytes": len(data),
                        "sha256": hashlib.sha256(data).hexdigest(),
                    }
                ),
            ),
        )
    store = GenerationStore(db)
    first, _ = store.begin(
        "alice", {"text": "question " * 100, "files": [{"file_id": "own"}]}
    )
    lease = ActorLease.claim(db, "alice", first.run_id)
    assert lease is not None
    store.finish(
        "alice",
        first.run_id,
        status="complete",
        text="answer " * 100,
        actor_lease=lease,
    )
    lease.release()
    job, _ = store.begin(
        "alice", {"conversationId": first.conversation_id, "text": "next"}
    )
    lease = ActorLease.claim(db, "alice", job.run_id)
    assert lease is not None
    with db.read_snapshot() as conn:
        canonical = generation_model_history(conn, "alice", job)
    return db, lease, file, canonical


@pytest.mark.parametrize("change", ["delete", "tamper", "pending", "owner"])
def test_file_changes_prevent_commit_and_summary_reuse(branch, change):
    db, lease, file, canonical = branch
    plan = plan_compaction(lease, retain_turns=1)
    assert plan is not None
    assert plan.canonical_fingerprint
    assert "verified-evidence" in str(plan.source)
    assert "verified-evidence" not in str(canonical)
    record = commit_compaction(
        lease, plan, "Earlier file evidence.", check_grant=lambda: None
    )
    assert record["sourceFingerprint"] == plan.canonical_fingerprint
    assert "Earlier file evidence." in str(project_compaction(lease, canonical))
    assert "verified-evidence" not in json.dumps(record)
    with db.connect() as conn:
        if change == "delete":
            conn.execute("DELETE FROM attachments WHERE id='own'")
        elif change == "pending":
            conn.execute(
                "INSERT INTO attachment_deletions(file_id,owner,original_path,quarantine_path,phase) VALUES(?,?,?,?,?)",
                ("own", "alice", str(file), "/quarantine", "prepared"),
            )
        elif change == "owner":
            conn.execute("UPDATE attachments SET owner='bob' WHERE id='own'")
        else:
            file.write_bytes(b"changed" * 200)
    assert project_compaction(lease, canonical) is canonical
    with pytest.raises(PermissionError, match="source or policy changed"):
        commit_compaction(
            lease, plan, "Earlier file evidence.", check_grant=lambda: None
        )
    new = plan_compaction(lease, retain_turns=1)
    assert new is not None
    assert new.fingerprint != plan.fingerprint
    assert new.canonical_fingerprint == plan.canonical_fingerprint
    assert "verified-evidence" not in str(new.source)


def test_media_compaction_real_provider_boundary(tmp_path, monkeypatch):
    runtime = runtime_for(tmp_path)
    db = provider(runtime)
    repo = WorkspaceRepository(db)
    ProviderRepository(db, CredentialCipher(db)).save(
        {
            "name": "test-provider",
            "provider": "openai",
            "models": ["test-model"],
            # Leave room for the default tool catalog even with the
            # optional-tokenizer UTF-8 byte upper bound.
            "model_contexts": {"test-model": 16384},
            "base_url": "https://models.invalid/v1",
            "api_key": "test-secret",
        }
    )
    repo.save_setting(
        "local-user",
        "endpoints",
        {
            "summarization": {
                "enabled": True,
                "retainRecent": {"turns": 1},
                "trigger": {"type": "messages_to_refine", "value": 2},
                "parameters": {"maxSummaryTokens": 128},
            }
        },
    )
    original = hosted_chat.run_authenticated_generation
    summaries, primary = [], []

    async def stream(profile, messages, **kwargs):
        if kwargs.get("emit_finish_reason"):
            summaries.append(messages)
            yield (
                "Attachment summary."
                if "secret-evidence" in str(messages)
                else "Current history."
            )
            yield ModelFinish("openai", "stop")
        else:
            primary.append(messages)
            yield "Answer " * 100

    async def execute(*args, **kwargs):
        return await original(
            *args, **kwargs, stream_tools=stream, stream_text=stream
        )

    monkeypatch.setattr(hosted_chat, "run_authenticated_generation", execute)
    prefix = "/api/ui/chat/generations"
    files = "/api/ui/chat/files"
    with TestClient(build_http_app(runtime=runtime)) as client:
        upload = client.post(
            files,
            files={
                "file": ("report.txt", b"secret-evidence " * 100, "text/plain")
            },
        )
        assert upload.status_code == 201
        ident = upload.json()["file_id"]
        body = {
            "text": "question " * 160,
            "provider": "test-provider",
            "model": "test-model",
        }
        first = client.post(
            prefix, json={**body, "files": [{"file_id": ident}]}
        )
        assert first.status_code == 202
        cid = first.json()["conversationId"]
        client.get(prefix + "/" + first.json()["runId"] + "/events")
        second = client.post(prefix, json={**body, "conversationId": cid})
        assert second.status_code == 202
        client.get(prefix + "/" + second.json()["runId"] + "/events")
        assert len(summaries) == 1
        assert "secret-evidence" in str(summaries[0])
        assert "Attachment summary." in str(primary[-1])
        assert "secret-evidence" not in str(primary[-1])
        assert (
            client.request(
                "DELETE", files, json={"file_ids": [ident]}
            ).status_code
            == 200
        )
        third = client.post(prefix, json={**body, "conversationId": cid})
        assert third.status_code == 202
        client.get(prefix + "/" + third.json()["runId"] + "/events")
        assert len(primary) == 3
        assert "Attachment summary." not in str(primary[-1])
        assert "secret-evidence" not in str(primary[-1])
        assert all(
            "secret-evidence" not in str(messages) for messages in summaries[1:]
        )
