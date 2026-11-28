"""Final upstream canonical file DTO and read-only history projection."""

import json


def _public_attachment_metadata(metadata):
    """Never surface unsanitized legacy HTML as a trusted office preview."""
    # The renderer uses this marker only for escaped, bounded Office HTML
    # created internally by publish_office_tool_output. Never forward a marker
    # from the stored metadata into the client DTO.
    trusted = metadata.get("trustedOfficeRenderer") == "escaped-office-v1"
    public_metadata = {
        key: value
        for key, value in metadata.items()
        if key
        not in {"extracted_text", "trustedOfficeRenderer", "path", "owner"}
    }
    if public_metadata.get("textFormat") == "html" and not trusted:
        public_metadata.pop("text", None)
        public_metadata.pop("textFormat", None)
        public_metadata["status"] = "failed"
        public_metadata["previewError"] = "unsupported"
    return public_metadata


def file_record(row):
    """Original React file DTO; physical paths stay in owner-scoped SQLite."""
    public_metadata = _public_attachment_metadata(json.loads(row["metadata"]))
    return {
        **public_metadata,
        "file_id": row["id"],
        "filename": row["name"],
        "type": row["content_type"],
        "filepath": "/api/ui/chat/files/download/" + row["id"],
        "source": "local",
        "createdAt": row["created_at"],
        "conversationId": row["conversation_id"],
    }


def _attachment(row, message_id, tool_call_id, conversation_id):
    return {
        **file_record(row),
        "messageId": message_id,
        "toolCallId": tool_call_id,
        "conversationId": conversation_id,
    }


def response_attachments(db, owner: str, message_ids):
    """Read canonical live file records, not stale message metadata snapshots."""
    ids = list(dict.fromkeys(message_ids))
    if not ids:
        return {}
    rows = db.execute(
        "SELECT l.response_message_id,l.tool_call_id,r.conversation_id,f.* "
        "FROM tool_attachment_links l JOIN runs r ON r.id=l.run_id "
        "JOIN conversations c ON c.id=r.conversation_id "
        "JOIN messages m ON m.id=l.response_message_id AND m.conversation_id=r.conversation_id "
        "JOIN attachments f ON f.id=l.file_id AND f.owner=c.owner "
        "WHERE c.owner=? AND l.response_message_id IN ("
        + ",".join("?" for _ in ids)
        + ") "
        "ORDER BY l.rowid",
        (owner, *ids),
    ).fetchall()
    collected = {ident: [] for ident in ids}
    for row in rows:
        collected[row["response_message_id"]].append(
            _attachment(
                row,
                row["response_message_id"],
                row["tool_call_id"],
                row["conversation_id"],
            )
        )
    # Automatic Memory artifacts have no physical file and no visible tool
    # call. The SSE event is persisted in the owning generation, so replay it
    # from that trusted event log instead of unverified message metadata.
    memory_rows = db.execute(
        "SELECT e.data,r.conversation_id,json_extract(r.state,'$.responseMessageId') AS response_message_id "
        "FROM runs r JOIN conversations c ON c.id=r.conversation_id "
        "JOIN messages m ON m.id=json_extract(r.state,'$.responseMessageId') AND m.conversation_id=r.conversation_id "
        "JOIN generation_events e ON e.run_id=r.id WHERE c.owner=? AND "
        "json_extract(r.state,'$.responseMessageId') IN ("
        + ",".join("?" for _ in ids)
        + ") "
        "AND json_extract(e.data,'$.event')='attachment' "
        "AND json_extract(e.data,'$.data.type')='memory' ORDER BY e.seq",
        (owner, *ids),
    ).fetchall()
    for row in memory_rows:
        event = json.loads(row["data"])
        value = event.get("data")
        if (
            not isinstance(value, dict)
            or not isinstance(value.get("memory"), dict)
            or value.get("messageId") != row["response_message_id"]
            or value.get("conversationId") != row["conversation_id"]
        ):
            continue
        # Old automatic-memory events mistakenly set toolCallId despite no
        # matching visible tool call. Keep them displayable after a reload.
        artifact = dict(value)
        artifact.pop("toolCallId", None)
        collected[row["response_message_id"]].append(artifact)
    return collected


def visible_response_attachments(
    files: list[dict], stored: object
) -> list[dict]:
    """Keep source MCP UI resources but authenticate file records via live links.

    Stored message metadata can outlive a deleted file. Never re-expose a
    legacy/unverified file descriptor by trusting the snapshot. UI resources
    are a separate (non-file) message attachment and survive cloned histories.
    """
    ui = []
    if isinstance(stored, list):
        for record in stored:
            if (
                isinstance(record, dict)
                and record.get("type") == "ui_resources"
                and isinstance(record.get("ui_resources"), list)
            ):
                ui.append(record)
    return [*files, *ui]


def message_file_ids(files: object) -> list[str]:
    """Normalize only client file identities, never client metadata or paths."""
    if files is None:
        return []
    if not isinstance(files, list) or len(files) > 32:
        raise ValueError("Invalid attachment list")
    ids = []
    for value in files:
        ident = value.get("file_id") if isinstance(value, dict) else None
        if not isinstance(ident, str) or not ident or len(ident) > 128:
            raise ValueError("Invalid attachment id")
        if ident not in ids:
            ids.append(ident)
    return ids


def resolve_message_files(db, owner: str, ids: list[str]) -> list[dict]:
    """Resolve references within the admission transaction, excluding deletion intents."""
    records = []
    for ident in ids:
        row = db.execute(
            "SELECT id,name,content_type FROM attachments WHERE id=? AND owner=? "
            "AND NOT EXISTS (SELECT 1 FROM attachment_deletions WHERE file_id=?)",
            (ident, owner, ident),
        ).fetchone()
        if row is None:
            raise KeyError("Attachment not found")
        records.append(
            {
                "file_id": row["id"],
                "filename": row["name"],
                "type": row["content_type"],
                "filepath": "/api/ui/chat/files/download/" + row["id"],
                "source": "local",
            }
        )
    return records
