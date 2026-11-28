"""Owned reference files are live, bounded provider evidence only."""

import hashlib
import json

from fastapi.testclient import TestClient

from tests.support.hosted_chat_http import provider, runtime_for
from workgate.control.http import hosted_chat
from workgate.control.http.app import build_http_app
from workgate.hosted_chat.agent_resources import (
    agent_context_messages,
    context_file_ids,
    with_agent_context,
)
from workgate.hosted_chat.agents import AgentRepository
from workgate.hosted_chat.database import SQLiteDatabase
from workgate.hosted_chat.storage import WorkspaceRepository

PREFIX = "/api/ui/chat/generations"


def test_resource_projection_fences(tmp_path):
    database = SQLiteDatabase(tmp_path)
    root = database.root / "attachments"
    root.mkdir(parents=True, exist_ok=True)
    ids = [
        "good",
        "missing",
        "foreign",
        "pending",
        "changed",
        "bad",
        "raw",
        "outside",
        "good",
    ]
    with database.connect() as db:
        for ident in ids[:-1]:
            path = root / ident if ident != "outside" else tmp_path / ident
            data = ("evidence-" + ident).encode()
            if ident != "missing":
                path.write_bytes(data)
            metadata = {
                "processed": ident != "raw",
                "extraction": "utf8",
                "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "extracted_text": data.decode(),
            }
            db.execute(
                "INSERT INTO attachments(id,owner,name,path,content_type,metadata) VALUES(?,?,?,?,?,?)",
                (
                    ident,
                    "bob" if ident == "foreign" else "alice",
                    ident + ".txt",
                    str(path),
                    "text/plain",
                    "[]" if ident == "bad" else json.dumps(metadata),
                ),
            )
        (root / "changed").write_bytes(b"x" * len("evidence-changed"))
        db.execute(
            "INSERT INTO attachment_deletions(file_id,owner,original_path,quarantine_path,phase) VALUES(?,?,?,?,?)",
            (
                "pending",
                "alice",
                str(root / "pending"),
                str(root / ".pending"),
                "prepared",
            ),
        )
    agent = {"tool_resources": {"context": {"file_ids": ids}}}
    with database.read_snapshot() as db:
        docs = agent_context_messages(database, db, "alice", agent)
    assert len(docs) == 1
    assert docs[0]["role"] == "user"
    assert "evidence-good" in docs[0]["content"]
    assert docs[0]["content"].count("evidence-") == 1
    assert str(root) not in docs[0]["content"]
    history = [
        {"role": "system", "content": "instructions"},
        {"role": "user", "content": "question"},
    ]
    projected = with_agent_context(history, docs)
    assert projected == [history[0], docs[0], history[1]]
    assert with_agent_context(projected, docs) == projected
    assert with_agent_context(history, []) is history
    assert context_file_ids(None) == []
    assert context_file_ids({"tool_resources": []}) == []
    assert context_file_ids({"tool_resources": {"context": []}}) == []
    assert context_file_ids(
        {"tool_resources": {"context": {"file_ids": [None, "", "a", "a"]}}}
    ) == ["a"]


def test_reference_evidence_only_reaches_provider(tmp_path, monkeypatch):
    runtime = runtime_for(tmp_path)
    db = provider(runtime)
    aid = AgentRepository(db).create(
        "local-user", {"provider": "test-provider", "model": "test-model"}
    )["id"]
    original = hosted_chat.run_authenticated_generation
    calls = []

    async def stream(profile, messages, **kwargs):
        calls.append(messages)
        yield "ordinary response"

    async def execute(*args, **kwargs):
        return await original(
            *args, **kwargs, stream_tools=stream, stream_text=stream
        )

    monkeypatch.setattr(hosted_chat, "run_authenticated_generation", execute)
    path = "/api/ui/chat/files/agent/" + aid
    with TestClient(build_http_app(runtime=runtime)) as client:
        uploaded = client.post(
            path,
            files={
                "file": (
                    "reference.txt",
                    b"Bearer private-document-evidence",
                    "text/plain",
                )
            },
        )
        assert uploaded.status_code == 201, uploaded.text
        ident = uploaded.json()["file_id"]
        payload = {
            "text": "question",
            "agent_id": aid,
            "generationProtocolVersion": 2,
        }
        result = client.post(PREFIX, json=payload)
        assert result.status_code == 202, result.text
        run = result.json()["runId"]
        response = client.get(PREFIX + "/" + run + "/events")
        assert "ordinary response" in response.text, response.text
        assert "private-document-evidence" not in response.text
        evidence = [
            m for m in calls[-1] if "private-document-evidence" in str(m)
        ]
        assert len(evidence) == 1 and evidence[0]["role"] == "user"
        assert "untrusted file content" in evidence[0]["content"]
        assert str(db.root) not in str(evidence)
        with db.read_snapshot() as connection:
            for table in ("messages", "runs"):
                assert "private-document-evidence" not in repr(
                    [
                        dict(row)
                        for row in connection.execute(f"SELECT * FROM {table}")
                    ]
                )
        WorkspaceRepository(db).save_setting(
            "local-user",
            "webui.filters.config",
            {"messages": {"pii": {"starterPatterns": ["bearer_header"]}}},
        )
        result = client.post(PREFIX, json=payload)
        assert result.status_code == 202, result.text
        run = result.json()["runId"]
        response = client.get(PREFIX + "/" + run + "/events")
        assert "private-document-evidence" not in response.text
        assert len(calls) == 1
        assert (
            client.request(
                "DELETE", path, json={"file_ids": [ident]}
            ).status_code
            == 200
        )
        result = client.post(PREFIX, json=payload)
        assert result.status_code == 202, result.text
        response = client.get(PREFIX + "/" + result.json()["runId"] + "/events")
        assert "ordinary response" in response.text
        assert len(calls) == 2
        assert "private-document-evidence" not in str(calls[-1])


def test_compaction_preserves_live_reference_without_summarizing_it(
    tmp_path, monkeypatch
):
    from workgate.hosted_chat.credential_cipher import CredentialCipher
    from workgate.hosted_chat.provider_common import ModelFinish
    from workgate.hosted_chat.provider_profiles import ProviderRepository

    runtime = runtime_for(tmp_path)
    db = provider(runtime)
    ProviderRepository(db, CredentialCipher(db)).save(
        {
            "name": "test-provider",
            "provider": "openai",
            "models": ["test-model"],
            "model_contexts": {"test-model": 8192},
            "base_url": "https://models.invalid/v1",
            "api_key": "test-secret",
        }
    )
    repo = WorkspaceRepository(db)
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
    aid = AgentRepository(db).create(
        "local-user", {"provider": "test-provider", "model": "test-model"}
    )["id"]
    original = hosted_chat.run_authenticated_generation
    summaries, primary = [], []

    async def stream(profile, messages, **kwargs):
        if kwargs.get("emit_finish_reason"):
            summaries.append(messages)
            yield "Earlier context."
            yield ModelFinish("openai", "stop")
        else:
            primary.append(messages)
            yield "Answer " * 100

    async def execute(*args, **kwargs):
        return await original(
            *args, **kwargs, stream_tools=stream, stream_text=stream
        )

    monkeypatch.setattr(hosted_chat, "run_authenticated_generation", execute)
    with TestClient(build_http_app(runtime=runtime)) as client:
        uploaded = client.post(
            "/api/ui/chat/files/agent/" + aid,
            files={
                "file": (
                    "reference.txt",
                    b"private-reference-marker",
                    "text/plain",
                )
            },
        )
        assert uploaded.status_code == 201
        cid = None
        for index in range(2):
            body = {
                "text": f"Question {index} " + "detail " * 160,
                "agent_id": aid,
            }
            if cid is not None:
                body["conversationId"] = cid
            result = client.post(PREFIX, json=body)
            assert result.status_code == 202, result.text
            cid = result.json()["conversationId"]
            response = client.get(
                PREFIX + "/" + result.json()["runId"] + "/events"
            )
            assert "Answer" in response.text
            assert "private-reference-marker" not in response.text
        assert len(summaries) == 1
        assert "private-reference-marker" not in str(summaries)
        assert len(primary) == 2
        assert all(
            str(messages).count("private-reference-marker") == 1
            for messages in primary
        )
        assert "Earlier context." in str(primary[-1])
        assert isinstance(cid, str)
        assert "private-reference-marker" not in str(
            repo.messages("local-user", cid)
        )
