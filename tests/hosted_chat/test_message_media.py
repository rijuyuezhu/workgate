"""Live attachment projection remains separate from durable conversation history."""

import base64
import hashlib
import json

import pytest
from fastapi.testclient import TestClient

from tests.support.hosted_chat_http import provider, runtime_for
from workgate.control.http import hosted_chat
from workgate.control.http.app import build_http_app
from workgate.hosted_chat import message_media
from workgate.hosted_chat.database import SQLiteDatabase
from workgate.hosted_chat.message_media import (
    detected_type,
    project_message_media,
)
from workgate.hosted_chat.storage import WorkspaceRepository


@pytest.fixture
def database(tmp_path):
    return SQLiteDatabase(tmp_path)


def save(database, ident, data, *, owner="alice", name="document.txt"):
    path = database.root.resolve() / "attachments" / ident
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    with database.connect() as db:
        db.execute(
            "INSERT INTO attachments(id,owner,name,path,content_type,metadata) VALUES(?,?,?,?,?,?)",
            (
                ident,
                owner,
                name,
                str(path),
                "application/forged",
                json.dumps(
                    {
                        "bytes": len(data),
                        "sha256": hashlib.sha256(data).hexdigest(),
                    }
                ),
            ),
        )
    return path


def user(*ids, text="prompt"):
    return {
        "role": "user",
        "content": text,
        "files": [{"file_id": ident} for ident in ids],
    }


def project(database, messages):
    with database.read_snapshot() as db:
        return project_message_media(database, db, "alice", messages)


@pytest.mark.parametrize(
    ("data", "mime"),
    [
        (b"\x89PNG\r\n\x1a\nxyz", "image/png"),
        (b"\xff\xd8\xffxyz", "image/jpeg"),
        (b"GIF87a", "image/gif"),
        (b"GIF89a", "image/gif"),
        (b"RIFFxxxxWEBPxyz", "image/webp"),
        (b"%PDF-1.7", "application/pdf"),
        (b"ordinary text", None),
    ],
)
def test_format_detection(data, mime):
    assert detected_type(data) == mime


def test_owned_bytes_and_original_order(database):
    png = b"\x89PNG\r\n\x1a\nimage"
    pdf = b"%PDF-1.7 document"
    save(database, "image", png)
    save(database, "doc", pdf, name="proof.pdf")
    save(database, "text", "中文".encode())
    messages = [
        user("image", "doc", "text"),
        {"role": "assistant", "content": "answer"},
    ]
    result = project(database, messages)
    assert result[0]["content"] == [
        {"type": "text", "text": "prompt"},
        {
            "type": "image_url",
            "image_url": {
                "url": "data:image/png;base64," + base64.b64encode(png).decode()
            },
        },
        {
            "type": "file",
            "file": {
                "filename": "proof.pdf",
                "file_data": "data:application/pdf;base64,"
                + base64.b64encode(pdf).decode(),
            },
        },
        {
            "type": "text",
            "text": "Attachment document.txt (untrusted file content):\n中文",
        },
    ]
    assert result[1] == messages[1]
    assert "files" not in result[0]
    assert messages[0] == user("image", "doc", "text")


def test_live_owner_deletion_integrity_and_invalid_types(database):
    changed = save(database, "changed", b"secret", name="changed.txt")
    changed.write_bytes(b"forged")
    save(
        database,
        "foreign",
        b"foreign secret",
        owner="bob",
        name="foreign-secret.txt",
    )
    save(database, "pending", b"pending secret", name="pending-secret.txt")
    save(database, "binary", b"\x00abc", name="binary")
    save(database, "invalid_utf8", b"\xffabc", name="invalid")
    save(
        database,
        "large",
        b"a" * (message_media.MAX_TEXT_BYTES + 1),
        name="large",
    )
    missing = save(database, "missing_file", b"missing secret")
    missing.unlink()
    save(database, "outside", b"outside secret")
    outside = database.root.parent / "outside"
    outside.write_bytes(b"outside secret")
    save(database, "invalid_meta", b"invalid secret")
    with database.connect() as db:
        db.execute(
            "INSERT INTO attachment_deletions(file_id,owner,original_path,quarantine_path,phase) VALUES(?,?,?,?,?)",
            ("pending", "alice", "/private", "/quarantine", "prepared"),
        )
        db.execute(
            "UPDATE attachments SET path=? WHERE id='outside'", (str(outside),)
        )
        db.execute(
            "UPDATE attachments SET metadata='{}' WHERE id='invalid_meta'"
        )
    result = project(
        database,
        [
            user(
                "changed",
                "foreign",
                "pending",
                "binary",
                "invalid_utf8",
                "large",
                "missing_file",
                "outside",
                "invalid_meta",
                "absent",
            )
        ],
    )
    rendered = json.dumps(result)
    for secret in (
        "forged",
        "foreign secret",
        "foreign-secret",
        "pending secret",
        "pending-secret",
        "outside secret",
        "invalid secret",
        "missing secret",
        str(database.root),
    ):
        assert secret not in rendered
    assert result[0]["content"][0] == {"type": "text", "text": "prompt"}
    assert len(result[0]["content"]) == 11
    assert result[0]["content"][2] == {
        "type": "text",
        "text": "Attachment unavailable.",
    }


def test_aggregate_budget_prefers_current_turn(database, monkeypatch):
    monkeypatch.setattr(message_media, "MAX_INLINE_BYTES", 6)
    monkeypatch.setattr(message_media, "MAX_ONE_INLINE_BYTES", 5)
    save(database, "old", b"old")
    save(database, "new", b"new")
    save(database, "latest", b"last")
    messages = [user("old"), user("new", "latest", text="current")]
    result = project(database, messages)
    assert result[1]["content"][1]["text"].endswith("new")
    assert "cannot be inlined" in result[1]["content"][2]["text"]
    assert result[0]["content"][1]["text"].endswith("old")
    monkeypatch.setattr(message_media, "MAX_PROJECTED_FILES", 1)
    limited = project(database, messages)
    assert "inline budget" in limited[0]["content"][1]["text"]
    assert "inline budget" in limited[1]["content"][2]["text"]


def test_http_provider_replay_and_policy(tmp_path, monkeypatch):
    runtime = runtime_for(tmp_path)
    db = provider(runtime)
    original = hosted_chat.run_authenticated_generation
    calls = []

    async def stream(profile, messages, **kwargs):
        calls.append(messages)
        yield "answer"

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
                "file": (
                    "document.txt",
                    b"Bearer private-file-evidence",
                    "text/plain",
                )
            },
        )
        assert upload.status_code == 201, upload.text
        ident = upload.json()["file_id"]
        payload = {
            "text": "question",
            "provider": "test-provider",
            "model": "test-model",
            "files": [{"file_id": ident}],
        }
        result = client.post(prefix, json=payload)
        assert result.status_code == 202, result.text
        run = result.json()["runId"]
        event = client.get(prefix + "/" + run + "/events")
        assert "answer" in event.text
        assert "private-file-evidence" not in event.text
        assert "private-file-evidence" in str(calls[-1])
        with db.read_snapshot() as connection:
            for table in ("messages", "runs", "generation_events"):
                assert "private-file-evidence" not in repr(
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
        result = client.post(
            prefix,
            json={
                **payload,
                "files": [],
                "conversationId": result.json()["conversationId"],
            },
        )
        assert result.status_code == 202, result.text
        assert (
            "private-file-evidence"
            not in client.get(
                prefix + "/" + result.json()["runId"] + "/events"
            ).text
        )
        assert len(calls) == 1
        assert (
            client.request(
                "DELETE", files, json={"file_ids": [ident]}
            ).status_code
            == 200
        )
        result = client.post(
            prefix,
            json={
                **payload,
                "files": [],
                "conversationId": result.json()["conversationId"],
            },
        )
        assert result.status_code == 202, result.text
        assert (
            "answer"
            in client.get(
                prefix + "/" + result.json()["runId"] + "/events"
            ).text
        )
        assert len(calls) == 2
        assert "Attachment unavailable." in str(calls[-1])
        assert "private-file-evidence" not in str(calls[-1])
