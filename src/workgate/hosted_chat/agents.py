"""LibreChat-compatible agent authoring and revision tracking in SQLite.

Source: upstream v6.0.3, webui/agents.py
(78384395bf93b00642c6a45fb7d7bbb7a3c01446). See LICENSE.upstream.
Owner-scoped authoring in the existing private Workgate SQLite database.
"""

import json
import secrets
from typing import Any

from .agent_validation import (
    clean_agent_config,
    sanitize_model_parameters,
)
from .database import SQLiteDatabase
from .storage import _cursor_decode, _cursor_encode, _like_pattern, _now


def _json(value: dict) -> str:
    # SQLite's JSON queries require actual JSON, not Python NaN/Infinity.
    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    )


class AgentRepository:
    def __init__(self, database: SQLiteDatabase):
        self.database = database

    @staticmethod
    def _from_row(row: Any) -> dict:
        value = json.loads(row["config"])
        value.update(
            {
                "id": row["id"],
                "_id": row["id"],
                "author": row["owner"],
                "name": row["name"],
                "description": row["description"],
                "created_at": int(row["created_at"] / 1_000_000),
                "createdAt": int(row["created_at"] / 1_000_000),
                "updatedAt": int(row["updated_at"] / 1_000_000),
                "version": int(value.get("_version", 1)),
                "isEditable": True,
            }
        )
        value.pop("_version", None)
        return value

    def create(self, owner: str, body: dict) -> dict:
        values = clean_agent_config(body, create=True)
        agent_id = "agent_" + secrets.token_hex(12)
        now = _now()
        values.setdefault("model_parameters", {})
        values.setdefault("conversation_starters", [])
        values.setdefault("category", "general")
        values["category"] = values["category"].strip()
        values["model_parameters"] = sanitize_model_parameters(
            values["model_parameters"]
        )
        values["_version"] = 1
        with self.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "INSERT INTO agents(id,owner,name,description,config,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                (
                    agent_id,
                    owner,
                    str(values.get("name") or ""),
                    str(values.get("description") or ""),
                    _json(values),
                    now,
                    now,
                ),
            )
            db.execute(
                "INSERT INTO agent_versions(agent_id,version,snapshot,created_at) VALUES(?,?,?,?)",
                (agent_id, 1, _json(values), now),
            )
            return self._from_row(
                db.execute(
                    "SELECT * FROM agents WHERE id=?", (agent_id,)
                ).fetchone()
            )

    def get(self, owner: str, agent_id: str) -> dict | None:
        with self.database.connect() as db:
            row = db.execute(
                "SELECT * FROM agents WHERE owner=? AND id=?", (owner, agent_id)
            ).fetchone()
            return self._from_row(row) if row else None

    def list(
        self,
        owner: str,
        *,
        limit: int = 25,
        cursor: str | None = None,
        category: str | None = None,
        search: str | None = None,
    ) -> dict:
        limit = max(1, min(int(limit), 100))
        filters = ["owner=?"]
        params: list[Any] = [owner]
        if category:
            filters.append("json_extract(config,'$.category')=?")
            params.append(category)
        if search:
            filters.append(
                "(name LIKE ? ESCAPE '\\' OR description LIKE ? ESCAPE '\\')"
            )
            params.extend([_like_pattern(search)] * 2)
        if cursor:
            decoded = _cursor_decode(cursor)
            assert decoded is not None
            stamp, ident = decoded
            filters.append("(updated_at<? OR (updated_at=? AND id<?))")
            params.extend([stamp, stamp, ident])
        with self.database.connect() as db:
            rows = db.execute(
                "SELECT * FROM agents WHERE "
                + " AND ".join(filters)
                + " ORDER BY updated_at DESC,id DESC LIMIT ?",
                (*params, limit + 1),
            ).fetchall()
        values = [self._from_row(row) for row in rows[:limit]]
        has_more = len(rows) > limit
        return {
            "object": "list",
            "data": values,
            "first_id": values[0]["id"] if values else "",
            "last_id": values[-1]["id"] if values else "",
            "has_more": has_more,
            **(
                {
                    "after": _cursor_encode(
                        rows[limit - 1]["updated_at"], rows[limit - 1]["id"]
                    )
                }
                if has_more
                else {}
            ),
        }

    def update(self, owner: str, agent_id: str, changes: dict) -> dict | None:
        values = clean_agent_config(changes)
        with self.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            return self._update_locked(db, owner, agent_id, values)

    def _update_locked(
        self,
        db: Any,
        owner: str,
        agent_id: str,
        values: dict,
        *,
        restore: bool = False,
    ) -> dict | None:
        """Apply one validated revision inside the caller's SQLite transaction."""
        row = db.execute(
            "SELECT * FROM agents WHERE owner=? AND id=?", (owner, agent_id)
        ).fetchone()
        if row is None:
            return None
        if "model_parameters" in values:
            values["model_parameters"] = sanitize_model_parameters(
                values["model_parameters"]
            )
        if "category" in values:
            values["category"] = values["category"].strip()
        config = json.loads(row["config"])
        if restore:
            previous_version = int(config.get("_version", 1))
            config = {**values, "_version": previous_version}
        else:
            config.update(values)
        version = int(config.get("_version", 1)) + 1
        config["_version"] = version
        now = _now()
        db.execute(
            "UPDATE agents SET name=?,description=?,config=?,updated_at=? WHERE id=? AND owner=?",
            (
                str(config.get("name") or ""),
                str(config.get("description") or ""),
                _json(config),
                now,
                agent_id,
                owner,
            ),
        )
        db.execute(
            "INSERT INTO agent_versions(agent_id,version,snapshot,created_at) VALUES(?,?,?,?)",
            (agent_id, version, _json(config), now),
        )
        return self._from_row(
            db.execute(
                "SELECT * FROM agents WHERE id=?", (agent_id,)
            ).fetchone()
        )

    def versions(self, owner: str, agent_id: str) -> list[dict] | None:
        with self.database.read_snapshot() as db:
            row = db.execute(
                "SELECT 1 FROM agents WHERE owner=? AND id=?", (owner, agent_id)
            ).fetchone()
            if row is None:
                return None
            versions = db.execute(
                "SELECT snapshot FROM agent_versions WHERE agent_id=? ORDER BY version DESC",
                (agent_id,),
            ).fetchall()
        snapshots = []
        for version in versions:
            payload = json.loads(version[0])
            payload["id"] = agent_id
            payload["version"] = payload.pop("_version", 1)
            snapshots.append(payload)
        return snapshots

    def revert(
        self, owner: str, agent_id: str, version_index: int
    ) -> dict | None:
        if (
            type(version_index) is not int
            or not 0 <= version_index <= 2**63 - 1
        ):
            raise ValueError("Unknown agent version")
        with self.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute(
                "SELECT 1 FROM agents WHERE owner=? AND id=?", (owner, agent_id)
            ).fetchone():
                return None
            row = db.execute(
                "SELECT snapshot FROM agent_versions WHERE agent_id=? ORDER BY version DESC LIMIT 1 OFFSET ?",
                (agent_id, version_index),
            ).fetchone()
            if row is None:
                raise ValueError("Unknown agent version")
            values = clean_agent_config(
                json.loads(row["snapshot"]), create=True
            )
            return self._update_locked(
                db, owner, agent_id, values, restore=True
            )

    def delete(self, owner: str, agent_id: str) -> bool:
        with self.database.connect() as db:
            return (
                db.execute(
                    "DELETE FROM agents WHERE id=? AND owner=?",
                    (agent_id, owner),
                ).rowcount
                > 0
            )

    def duplicate(self, owner: str, agent_id: str) -> dict | None:
        current = self.get(owner, agent_id)
        if current is None:
            return None
        values = clean_agent_config(current)
        values["name"] = (values.get("name") or "Agent") + " (copy)"
        duplicate = self.create(owner, values)
        return {"agent": duplicate, "actions": []}
