"""Conversation, project, agent and message persistence in the controller DB.

The repository speaks in LibreChat's camelCase identifiers and preserves
unknown non-private fields to avoid destroying information during migrations.
"""

import base64
import json
import secrets
import time
import uuid
from datetime import UTC, datetime
from typing import Any

from .attachment_history import (
    response_attachments,
    visible_response_attachments,
)
from .database import SQLiteDatabase


def _stamp(value: int | float) -> str:
    return (
        datetime.fromtimestamp(value, tz=UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _now() -> int:
    return time.time_ns() // 1000


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _decode(value: str | bytes) -> dict:
    parsed = json.loads(value)
    return parsed if isinstance(parsed, dict) else {}


def _like_pattern(value: str) -> str:
    return (
        "%"
        + value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        + "%"
    )


def _cursor_encode(timestamp: int, identifier: str) -> str:
    return base64.urlsafe_b64encode(
        _json({"stamp": timestamp, "id": identifier}).encode()
    ).decode()


def _cursor_decode(cursor: str | None) -> tuple[int, str] | None:
    if cursor is None:
        return None
    try:
        if len(cursor) > 4096:
            raise ValueError("Invalid pagination cursor")
        data = json.loads(
            base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        )
        stamp, identifier = data["stamp"], data["id"]
        if (
            type(stamp) is not int
            or not 0 <= stamp <= 2**63 - 1
            or not isinstance(identifier, str)
            or not identifier
        ):
            raise ValueError("Invalid pagination cursor")
        return stamp, identifier
    except (
        ValueError,
        KeyError,
        UnicodeDecodeError,
        TypeError,
        OverflowError,
    ) as exc:
        raise ValueError("Invalid pagination cursor") from exc


class WorkspaceRepository:
    def __init__(self, database: SQLiteDatabase):
        self.database = database

    @staticmethod
    def _validate_references(db: Any, owner: str, values: dict) -> None:
        for key, table in (("projectId", "projects"), ("agent_id", "agents")):
            ident = values.get(key)
            if ident is not None and (
                not isinstance(ident, str)
                or not db.execute(
                    f"SELECT 1 FROM {table} WHERE id=? AND owner=?",
                    (ident, owner),
                ).fetchone()
            ):
                raise KeyError(f"Unknown {key}")

    @staticmethod
    def _convo(row: Any) -> dict:
        data = _decode(row["metadata"])
        data.update(
            conversationId=row["id"],
            title=row["title"],
            projectId=row["project_id"],
            chatProjectId=row["project_id"],
            agent_id=row["agent_id"],
            model=row["model"],
            endpoint=row["endpoint"],
            isArchived=bool(row["archived"]),
            pinned=bool(row["pinned"]),
            createdAt=_stamp(row["created_at"] / 1_000_000),
            updatedAt=_stamp(row["updated_at"] / 1_000_000),
        )
        # Retained convoSchema permits an absent Agent but rejects null.
        if row["agent_id"] is None:
            data.pop("agent_id", None)
        return data

    @staticmethod
    def _project(row: Any) -> dict:
        data = _decode(row["metadata"])
        data.update(
            _id=row["id"],
            id=row["id"],
            user=row["owner"],
            name=row["name"],
            description=row["description"],
            createdAt=_stamp(row["created_at"] / 1_000_000),
            updatedAt=_stamp(row["updated_at"] / 1_000_000),
        )
        return data

    @staticmethod
    def _message(row: Any) -> dict:
        data = _decode(row["metadata"])
        data.update(
            messageId=row["id"],
            conversationId=row["conversation_id"],
            parentMessageId=row["parent_id"],
            role=row["role"],
            text=row["text"],
            createdAt=_stamp(row["created_at"] / 1_000_000),
            updatedAt=_stamp(row["updated_at"] / 1_000_000),
        )
        content = json.loads(row["content"])
        if isinstance(content, (dict, list)) and content:
            data["content"] = content
        return data

    def create_conversation(self, owner: str, payload: dict) -> dict:
        conversation_id = str(payload.get("conversationId") or uuid.uuid4())
        title = str(payload.get("title") or "New chat")[:500]
        metadata = {
            key: value
            for key, value in payload.items()
            if key
            not in {
                "conversationId",
                "title",
                "projectId",
                "agent_id",
                "model",
                "endpoint",
                "isArchived",
                "pinned",
            }
        }
        now = _now()
        with self.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute(
                "SELECT 1 FROM active_subagent_threads WHERE thread_id=?",
                (conversation_id,),
            ).fetchone():
                raise PermissionError("Child conversation is read-only")
            self._validate_references(db, owner, payload)
            existing = db.execute(
                "SELECT 1 FROM conversations WHERE id=?", (conversation_id,)
            ).fetchone()
            if existing:
                raise ValueError("Conversation already exists")
            db.execute(
                "INSERT INTO conversations(id,owner,title,project_id,agent_id,model,endpoint,archived,pinned,metadata,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    conversation_id,
                    owner,
                    title,
                    payload.get("projectId"),
                    payload.get("agent_id"),
                    payload.get("model"),
                    payload.get("endpoint"),
                    int(bool(payload.get("isArchived"))),
                    int(bool(payload.get("pinned"))),
                    _json(metadata),
                    now,
                    now,
                ),
            )
            return self._convo(
                db.execute(
                    "SELECT * FROM conversations WHERE id=?", (conversation_id,)
                ).fetchone()
            )

    def conversation(self, owner: str, conversation_id: str) -> dict | None:
        with self.database.connect() as db:
            row = db.execute(
                "SELECT * FROM conversations WHERE id=? AND owner=?",
                (conversation_id, owner),
            ).fetchone()
            return self._convo(row) if row else None

    def conversations(
        self,
        owner: str,
        *,
        archived: bool = False,
        pinned: bool = False,
        project_id: str | None = None,
        search: str | None = None,
        tags: list[str] | None = None,
        cursor: str | None = None,
        limit: int = 25,
    ) -> dict:
        limit = max(1, min(int(limit), 100))
        filters = ["owner=?", "archived=?"]
        params: list[Any] = [owner, int(archived)]
        if pinned:
            filters.append("pinned=1")
        if project_id == "unassigned":
            filters.append("project_id IS NULL")
        elif project_id:
            filters.append("project_id=?")
            params.append(project_id)
        if tags:
            # Bookmark filtering must happen before cursor pagination. JSON
            # array membership avoids substring/LIKE wildcard collisions.
            # Multiple selected bookmarks are an OR filter, matching the
            # conversation sidebar's existing filter semantics.
            placeholders = ",".join("?" for _ in tags)
            filters.append(
                "EXISTS (SELECT 1 FROM json_each(conversations.metadata, '$.tags') "
                f"WHERE json_each.value IN ({placeholders}))"
            )
            params.extend(tags)
        if search:
            escaped = (
                search.replace("\\", "\\\\")
                .replace("%", "\\%")
                .replace("_", "\\_")
            )
            filters.append(
                "(title LIKE ? ESCAPE '\\' OR id IN "
                "(SELECT conversation_id FROM messages WHERE text LIKE ? ESCAPE '\\'))"
            )
            params += [f"%{escaped}%", f"%{escaped}%"]
        last = _cursor_decode(cursor)
        if last:
            filters.append("(updated_at < ? OR (updated_at = ? AND id < ?))")
            params.extend([last[0], last[0], last[1]])
        sql = (
            "SELECT * FROM conversations WHERE "
            + " AND ".join(filters)
            + " ORDER BY updated_at DESC, id DESC LIMIT ?"
        )
        with self.database.connect() as db:
            rows = db.execute(sql, [*params, limit + 1]).fetchall()
        has_next = len(rows) > limit
        result = rows[:limit]
        return {
            "conversations": [self._convo(r) for r in result],
            "nextCursor": _cursor_encode(
                result[-1]["updated_at"], result[-1]["id"]
            )
            if has_next and result
            else None,
        }

    def update_conversation(
        self, owner: str, conversation_id: str, changes: dict
    ) -> dict | None:
        allowed = {
            "title": "title",
            "projectId": "project_id",
            "agent_id": "agent_id",
            "model": "model",
            "endpoint": "endpoint",
            "isArchived": "archived",
            "pinned": "pinned",
        }
        with self.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute(
                "SELECT 1 FROM active_subagent_threads WHERE thread_id=? AND owner=?",
                (conversation_id, owner),
            ).fetchone():
                raise PermissionError("Child conversation is read-only")
            row = db.execute(
                "SELECT * FROM conversations WHERE id=? AND owner=?",
                (conversation_id, owner),
            ).fetchone()
            if row is None:
                return None
            self._validate_references(db, owner, changes)
            updates = {
                allowed[k]: int(bool(v)) if k in {"isArchived", "pinned"} else v
                for k, v in changes.items()
                if k in allowed
            }
            metadata = _decode(row["metadata"])
            metadata.update(
                {
                    key: value
                    for key, value in changes.items()
                    if key not in allowed and key != "conversationId"
                }
            )
            updates["metadata"] = _json(metadata)
            updates["updated_at"] = _now()
            values = list(updates.values())
            db.execute(
                "UPDATE conversations SET "
                + ",".join(f"{name}=?" for name in updates)
                + " WHERE id=? AND owner=?",
                [*values, conversation_id, owner],
            )
            return self._convo(
                db.execute(
                    "SELECT * FROM conversations WHERE id=? AND owner=?",
                    (conversation_id, owner),
                ).fetchone()
            )

    def delete_conversation(
        self, owner: str, conversation_id: str, *, require_idle: bool = False
    ) -> bool:
        with self.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if (
                require_idle
                and db.execute(
                    "SELECT 1 FROM runs r JOIN conversations c ON c.id=r.conversation_id WHERE c.owner=? AND c.id=? AND r.status IN ('running','requires_action')",
                    (owner, conversation_id),
                ).fetchone()
            ):
                raise PermissionError(
                    "Abort the active generation before deleting its conversation"
                )
            if db.execute(
                "SELECT 1 FROM active_subagent_threads WHERE thread_id=? AND owner=? AND active=1",
                (conversation_id, owner),
            ).fetchone():
                raise PermissionError("Child conversation is read-only")
            if db.execute(
                "SELECT 1 FROM subagent_tasks WHERE owner=? AND parent_conversation_id=? AND status='running'",
                (owner, conversation_id),
            ).fetchone():
                raise PermissionError(
                    "Conversation has running detached children"
                )
            if db.execute(
                "SELECT 1 FROM agent_event_bindings b JOIN active_subagent_threads a ON a.owner=b.owner AND a.thread_id=b.conversation_id AND a.active=1 "
                "WHERE b.owner=? AND b.parent_conversation_id=?",
                (owner, conversation_id),
            ).fetchone():
                raise PermissionError("Conversation has active event bindings")
            result = db.execute(
                "DELETE FROM conversations WHERE owner=? AND id=?",
                (owner, conversation_id),
            )
            return result.rowcount > 0

    def unread(self, owner: str) -> dict[str, str]:
        with self.database.read_snapshot() as db:
            return {
                row["id"]: row["unread_marker"]
                for row in db.execute(
                    "SELECT id,unread_marker FROM conversations WHERE owner=? AND unread_marker IS NOT NULL",
                    (owner,),
                )
            }

    def set_unread(
        self,
        owner: str,
        conversation_id: str,
        *,
        unread: bool,
        expected: str | None = None,
    ) -> str | None:
        """A read acknowledgment cannot erase a newer generation's marker."""
        with self.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT unread_marker FROM conversations WHERE owner=? AND id=?",
                (owner, conversation_id),
            ).fetchone()
            if row is None:
                raise KeyError("Conversation not found")
            marker = row["unread_marker"]
            if unread:
                marker = "manual:" + uuid.uuid4().hex
            elif marker == expected:
                marker = None
            db.execute(
                "UPDATE conversations SET unread_marker=? WHERE owner=? AND id=?",
                (marker, owner, conversation_id),
            )
            return marker

    def add_message(
        self, owner: str, conversation_id: str, payload: dict
    ) -> dict:
        message_id = str(payload.get("messageId") or uuid.uuid4())
        metadata = {
            key: value
            for key, value in payload.items()
            if key
            not in {
                "messageId",
                "conversationId",
                "parentMessageId",
                "role",
                "text",
                "content",
            }
        }
        now = _now()
        with self.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute(
                "SELECT 1 FROM active_subagent_threads WHERE thread_id=?",
                (conversation_id,),
            ).fetchone():
                raise PermissionError("Child conversation is read-only")
            if not db.execute(
                "SELECT 1 FROM conversations WHERE id=? AND owner=?",
                (conversation_id, owner),
            ).fetchone():
                raise KeyError("Conversation not found")
            db.execute(
                "INSERT INTO messages(id,conversation_id,parent_id,role,text,content,metadata,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    message_id,
                    conversation_id,
                    payload.get("parentMessageId"),
                    str(payload.get("role", "user")),
                    str(payload.get("text") or ""),
                    _json(payload.get("content") or {}),
                    _json(metadata),
                    now,
                    now,
                ),
            )
            db.execute(
                "UPDATE conversations SET updated_at=? WHERE id=?",
                (now, conversation_id),
            )
            return self._message(
                db.execute(
                    "SELECT * FROM messages WHERE id=?", (message_id,)
                ).fetchone()
            )

    def messages(
        self,
        owner: str,
        conversation_id: str,
        *,
        limit: int = 100,
        cursor: str | None = None,
    ) -> dict:
        limit = max(1, min(int(limit), 200))
        with self.database.read_snapshot() as db:
            if not db.execute(
                "SELECT 1 FROM conversations WHERE id=? AND owner=?",
                (conversation_id, owner),
            ).fetchone():
                raise KeyError("Conversation not found")
            params: list[Any] = [conversation_id]
            where = "conversation_id=?"
            previous = _cursor_decode(cursor)
            if previous is not None:
                where += " AND (created_at < ? OR (created_at=? AND id < ?))"
                params.extend([previous[0], previous[0], previous[1]])
            rows = db.execute(
                f"SELECT * FROM messages WHERE {where} ORDER BY created_at DESC,id DESC LIMIT ?",
                [*params, limit + 1],
            ).fetchall()
            has_next = len(rows) > limit
            page = rows[:limit]
            messages = self._project_messages(db, owner, list(reversed(page)))
        return {
            "messages": messages,
            "nextCursor": _cursor_encode(page[-1]["created_at"], page[-1]["id"])
            if has_next
            else None,
        }

    def _project_messages(self, db: Any, owner: str, rows: list) -> list[dict]:
        current = response_attachments(
            db, owner, [row["id"] for row in rows if row["role"] == "assistant"]
        )
        messages = []
        for row in rows:
            message = self._message(row)
            if row["role"] == "assistant" and (
                row["id"] in current or "attachments" in message
            ):
                message["attachments"] = visible_response_attachments(
                    current.get(row["id"], []), message.get("attachments")
                )
            messages.append(message)
        return messages

    def search_messages(
        self, owner: str, query: str, limit: int = 30
    ) -> list[dict]:
        if not query.strip():
            return []
        with self.database.read_snapshot() as db:
            if len(query) < 3 or not getattr(
                self.database, "fts_enabled", False
            ):
                rows = db.execute(
                    "SELECT m.* FROM messages m JOIN conversations c ON c.id=m.conversation_id "
                    "WHERE c.owner=? AND m.text LIKE ? ESCAPE '\\' ORDER BY m.created_at DESC,m.id DESC LIMIT ?",
                    (owner, _like_pattern(query), max(1, min(int(limit), 100))),
                ).fetchall()
            else:
                # MATCH can interpret operators; quote the literal user search term.
                escaped = '"' + query.replace('"', '""') + '"'
                rows = db.execute(
                    "SELECT m.* FROM message_search s JOIN messages m ON m.id=s.message_id "
                    "JOIN conversations c ON c.id=m.conversation_id "
                    "WHERE c.owner=? AND s.text MATCH ? ORDER BY m.created_at DESC,m.id DESC LIMIT ?",
                    (owner, escaped, max(1, min(int(limit), 100))),
                ).fetchall()
            return self._project_messages(db, owner, rows)

    def create_project(
        self, owner: str, name: str, description: str = "", **metadata: Any
    ) -> dict:
        name = name.strip()[:160]
        if not name:
            raise ValueError("Project name is required")
        project_id = secrets.token_hex(12)
        now = _now()
        with self.database.connect() as db:
            db.execute(
                "INSERT INTO projects(id,owner,name,description,metadata,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                (
                    project_id,
                    owner,
                    name,
                    description[:4000],
                    _json(metadata),
                    now,
                    now,
                ),
            )
            return self._project(
                db.execute(
                    "SELECT * FROM projects WHERE id=?", (project_id,)
                ).fetchone()
            )

    def project(self, owner: str, project_id: str) -> dict | None:
        with self.database.connect() as db:
            row = db.execute(
                "SELECT * FROM projects WHERE owner=? AND id=?",
                (owner, project_id),
            ).fetchone()
            return self._project(row) if row else None

    def projects(
        self, owner: str, *, limit: int = 30, search: str | None = None
    ) -> dict:
        with self.database.connect() as db:
            if search:
                rows = db.execute(
                    "SELECT * FROM projects WHERE owner=? AND (name LIKE ? ESCAPE '\\' OR description LIKE ? ESCAPE '\\') "
                    "ORDER BY updated_at DESC LIMIT ?",
                    (
                        owner,
                        _like_pattern(search),
                        _like_pattern(search),
                        max(1, min(int(limit), 100)),
                    ),
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT * FROM projects WHERE owner=? ORDER BY updated_at DESC LIMIT ?",
                    (owner, max(1, min(int(limit), 100))),
                ).fetchall()
        return {
            "projects": [self._project(row) for row in rows],
            "nextCursor": None,
        }

    def update_project(
        self, owner: str, project_id: str, payload: dict
    ) -> dict | None:
        with self.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM projects WHERE owner=? AND id=?",
                (owner, project_id),
            ).fetchone()
            if not row:
                return None
            metadata = _decode(row["metadata"])
            metadata.update(
                {
                    key: value
                    for key, value in payload.items()
                    if key not in {"name", "description"}
                }
            )
            name = str(payload.get("name", row["name"])).strip()[:160]
            if not name:
                raise ValueError("Project name is required")
            db.execute(
                "UPDATE projects SET name=?,description=?,metadata=?,updated_at=? WHERE id=? AND owner=?",
                (
                    name,
                    str(payload.get("description", row["description"]))[:4000],
                    _json(metadata),
                    _now(),
                    project_id,
                    owner,
                ),
            )
            return self._project(
                db.execute(
                    "SELECT * FROM projects WHERE id=?", (project_id,)
                ).fetchone()
            )

    def delete_project(self, owner: str, project_id: str) -> bool:
        with self.database.connect() as db:
            return (
                db.execute(
                    "DELETE FROM projects WHERE id=? AND owner=?",
                    (project_id, owner),
                ).rowcount
                > 0
            )

    def save_setting(self, owner: str, key: str, value: Any) -> None:
        with self.database.connect() as db:
            db.execute(
                "INSERT INTO settings(owner,key,value) VALUES(?,?,?) ON CONFLICT(owner,key) "
                "DO UPDATE SET value=excluded.value",
                (owner, key, _json(value)),
            )

    def merge_setting(
        self, owner: str, key: str, changes: dict, *, max_bytes: int
    ) -> dict:
        """Merge a preference patch without losing concurrent updates to other keys."""
        with self.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT value FROM settings WHERE owner=? AND key=?",
                (owner, key),
            ).fetchone()
            current = json.loads(row[0]) if row else {}
            merged = {
                **(current if isinstance(current, dict) else {}),
                **changes,
            }
            encoded = _json(merged)
            if len(encoded.encode("utf-8")) > max_bytes:
                raise ValueError("Preferences are too large")
            db.execute(
                "INSERT INTO settings(owner,key,value) VALUES(?,?,?) ON CONFLICT(owner,key) "
                "DO UPDATE SET value=excluded.value",
                (owner, key, encoded),
            )
            return merged

    def setting(self, owner: str, key: str, default: Any = None) -> Any:
        with self.database.connect() as db:
            row = db.execute(
                "SELECT value FROM settings WHERE owner=? AND key=?",
                (owner, key),
            ).fetchone()
            return json.loads(row[0]) if row else default
