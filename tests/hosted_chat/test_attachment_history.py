"""Pinned attachment projection contracts without unimported generation routes."""

import json

from workgate.hosted_chat.database import SQLiteDatabase
from workgate.hosted_chat.storage import WorkspaceRepository


def test_history_uses_canonical_owned_records_and_no_deleted_snapshots(
    tmp_path,
):
    database = SQLiteDatabase(tmp_path)
    repo = WorkspaceRepository(database)
    cid = repo.create_conversation("alice", {})["conversationId"]
    other = repo.create_conversation("bob", {})["conversationId"]
    ui = {"type": "ui_resources", "ui_resources": []}
    stored = [{"file_id": "stale"}, ui, None]
    mid = repo.add_message(
        "alice",
        cid,
        {"role": "assistant", "text": "generated file", "attachments": stored},
    )["messageId"]
    with database.connect() as db:
        db.execute(
            "INSERT INTO runs(id,conversation_id,status,state) VALUES(?,?,?,?)",
            ("r", cid, "complete", json.dumps({"responseMessageId": mid})),
        )
        db.execute(
            "INSERT INTO runs(id,conversation_id,status,state) VALUES(?,?,?,?)",
            (
                "wrong",
                other,
                "complete",
                json.dumps({"responseMessageId": mid}),
            ),
        )
        for ident, owner in (("own", "alice"), ("foreign", "bob")):
            metadata = {
                "status": "ready",
                "textFormat": "html",
                "text": "<script>bad</script>",
                "extracted_text": "private",
                "path": "private",
            }
            db.execute(
                "INSERT INTO attachments(id,owner,name,path,metadata) VALUES(?,?,?,?,?)",
                (ident, owner, "file.txt", "/private", json.dumps(metadata)),
            )
            db.execute(
                "INSERT INTO tool_attachment_links(run_id,response_message_id,tool_call_id,file_id) VALUES(?,?,?,?)",
                ("r", mid, ident, ident),
            )
        db.execute(
            "INSERT INTO tool_attachment_links(run_id,response_message_id,tool_call_id,file_id) VALUES(?,?,?,?)",
            ("wrong", mid, "wrong", "foreign"),
        )
        for memory in (
            {
                "messageId": mid,
                "conversationId": cid,
                "type": "memory",
                "memory": {"content": "saved"},
                "toolCallId": "legacy",
            },
            {
                "messageId": "wrong",
                "conversationId": cid,
                "type": "memory",
                "memory": {},
            },
        ):
            db.execute(
                "INSERT INTO generation_events(run_id,data) VALUES(?,?)",
                ("r", json.dumps({"event": "attachment", "data": memory})),
            )
    result = repo.messages("alice", cid)["messages"][0]["attachments"]
    assert len(result) == 3 and result[-1] == ui
    file = result[0]
    assert file["file_id"] == "own" and file["status"] == "failed"
    assert (
        not {"path", "text", "extracted_text", "trustedOfficeRenderer"}
        & file.keys()
    )
    assert result[1]["type"] == "memory" and "toolCallId" not in result[1]
    assert (
        repo.search_messages("alice", "generated")[0]["attachments"] == result
    )
    assert repo.search_messages("bob", "generated") == []
    with database.connect() as db:
        db.execute("DELETE FROM attachments WHERE id='own'")
    assert (
        repo.messages("alice", cid)["messages"][0]["attachments"] == result[1:]
    )
    assert (
        repo.search_messages("alice", "generated")[0]["attachments"]
        == result[1:]
    )
