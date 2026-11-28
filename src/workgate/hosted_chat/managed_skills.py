"""Owner-managed chat Skill records; deployed Skills remain in Agent Bridge."""

import json
import re
import sqlite3
import uuid

from ..agent_bridge.models import validate_skill_name
from .content_filters import (
    ContentFragment,
    filters_from_snapshot,
    inspect_content,
)
from .database import SQLiteDatabase
from .storage import _now, _stamp

_MAX_BODY = 512 * 1024
_MAX_SKILLS = 256
_STATE_KEY = "webui.skill.states"
_FIELDS = frozenset(
    {
        "name",
        "displayTitle",
        "description",
        "body",
        "frontmatter",
        "category",
        "alwaysApply",
    }
)
_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,127}")


class SkillConflict(ValueError):
    pass


def _json(value) -> str:
    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    )


def validate_record(value: dict) -> dict:
    if not isinstance(value, dict) or set(value) - _FIELDS:
        raise ValueError("Invalid Skill record")
    name = value.get("name")
    if not isinstance(name, str):
        raise ValueError("Invalid Skill name")
    validate_skill_name(name)
    if not _NAME.fullmatch(name):
        raise ValueError("Skill name must be lowercase kebab-case")
    description = value.get("description")
    body = value.get("body")
    if (
        not isinstance(description, str)
        or not description.strip()
        or len(description) > 500
    ):
        raise ValueError("Invalid Skill description")
    if not isinstance(body, str) or len(body.encode("utf-8")) > _MAX_BODY:
        raise ValueError("Invalid Skill body")
    for key, limit in (("displayTitle", 256), ("category", 128)):
        item = value.get(key)
        if item is not None and (
            not isinstance(item, str) or len(item) > limit
        ):
            raise ValueError("Invalid Skill " + key)
    front = value.get("frontmatter", {})
    try:
        if (
            not isinstance(front, dict)
            or len(_json(front).encode("utf-8")) > 32768
        ):
            raise ValueError("Invalid Skill frontmatter")
    except TypeError, RecursionError, UnicodeError:
        raise ValueError("Invalid Skill frontmatter") from None
    for flag in ("user-invocable", "disable-model-invocation"):
        if flag in front and type(front[flag]) is not bool:
            raise ValueError("Invalid Skill frontmatter flag")
    allowed = front.get("allowed-tools", [])
    if (
        not isinstance(allowed, list)
        or len(allowed) > 128
        or any(
            not isinstance(tool, str) or not 1 <= len(tool) <= 256
            for tool in allowed
        )
    ):
        raise ValueError("Invalid Skill allowed-tools metadata")
    always = value.get("alwaysApply", False)
    if type(always) is not bool:
        raise ValueError("Invalid Skill alwaysApply")
    record = {**value, "frontmatter": front, "alwaysApply": always}
    _json(record).encode("utf-8")
    return record


def enforce_policy(db, owner: str, *, description="", body="", references=()):
    fragments = [
        ContentFragment("skill", "description", description),
        ContentFragment("skill", "body", body),
        *(ContentFragment("skill", "body", text) for text in references),
    ]
    blocked = inspect_content(fragments, filters_from_snapshot(db, owner))
    if blocked is not None:
        raise blocked


def owned_skill(db, owner, sid):
    row = db.execute(
        "SELECT * FROM webui_skills WHERE owner=? AND id=?", (owner, sid)
    ).fetchone()
    if row is None:
        raise KeyError("Skill not found")
    return row


def present_skill(row, *, detail=False, file_count=0):
    record = json.loads(row["record"])
    front = record.get("frontmatter", {})
    result = {
        **record,
        "_id": row["id"],
        "author": row["owner"],
        "authorName": "WorkGate",
        "version": row["version"],
        "source": "inline",
        "fileCount": file_count,
        "userInvocable": front.get("user-invocable", True),
        "disableModelInvocation": front.get("disable-model-invocation", False),
        "allowedTools": front.get("allowed-tools", []),
        "createdAt": _stamp(row["created_at"] / 1_000_000),
        "updatedAt": _stamp(row["updated_at"] / 1_000_000),
    }
    if not detail:
        result.pop("body", None)
        result.pop("frontmatter", None)
    return result


def skill_states(db, owner):
    row = db.execute(
        "SELECT value FROM settings WHERE owner=? AND key=?",
        (owner, _STATE_KEY),
    ).fetchone()
    value = json.loads(row[0]) if row else {}
    return value if isinstance(value, dict) else {}


def _save_states(db, owner, states):
    db.execute(
        "INSERT INTO settings(owner,key,value) VALUES(?,?,?) ON CONFLICT(owner,key) DO UPDATE SET value=excluded.value",
        (owner, _STATE_KEY, _json(states)),
    )


class ManagedSkills:
    def __init__(self, database: SQLiteDatabase):
        self.database = database

    def create(self, owner: str, payload: dict) -> dict:
        record = validate_record(payload)
        now = _now()
        sid = str(uuid.uuid4())
        with self.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if (
                db.execute(
                    "SELECT count(*) FROM webui_skills WHERE owner=?", (owner,)
                ).fetchone()[0]
                >= _MAX_SKILLS
            ):
                raise SkillConflict("Managed Skill limit reached")
            enforce_policy(
                db,
                owner,
                description=record["description"],
                body=record["body"],
            )
            try:
                db.execute(
                    "INSERT INTO webui_skills(id,owner,name,record,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                    (sid, owner, record["name"], _json(record), now, now),
                )
            except sqlite3.IntegrityError as error:
                raise SkillConflict("Skill name already exists") from error
            return present_skill(owned_skill(db, owner, sid), detail=True)

    def list(self, owner: str) -> list[dict]:
        with self.database.read_snapshot() as db:
            rows = db.execute(
                "SELECT s.*,(SELECT count(*) FROM webui_skill_files f WHERE f.skill_id=s.id AND f.owner=s.owner) AS file_count "
                "FROM webui_skills s WHERE s.owner=? ORDER BY s.updated_at DESC,s.id LIMIT ?",
                (owner, _MAX_SKILLS),
            ).fetchall()
            return [
                present_skill(row, file_count=row["file_count"]) for row in rows
            ]

    def get(self, owner: str, sid: str) -> dict:
        with self.database.read_snapshot() as db:
            row = owned_skill(db, owner, sid)
            count = db.execute(
                "SELECT count(*) FROM webui_skill_files WHERE skill_id=? AND owner=?",
                (sid, owner),
            ).fetchone()[0]
            return present_skill(row, detail=True, file_count=count)

    def update(self, owner: str, sid: str, patch: dict) -> dict:
        if (
            not isinstance(patch, dict)
            or set(patch) - (_FIELDS | {"expectedVersion"})
            or type(patch.get("expectedVersion")) is not int
        ):
            raise ValueError("Skill update requires an expected version")
        with self.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = owned_skill(db, owner, sid)
            if row["version"] != patch["expectedVersion"]:
                raise SkillConflict("Skill version changed")
            record = validate_record(
                {
                    **json.loads(row["record"]),
                    **{
                        k: v for k, v in patch.items() if k != "expectedVersion"
                    },
                }
            )
            enforce_policy(
                db,
                owner,
                description=record["description"],
                body=record["body"],
            )
            try:
                db.execute(
                    "UPDATE webui_skills SET name=?,record=?,version=version+1,updated_at=? WHERE id=? AND owner=?",
                    (record["name"], _json(record), _now(), sid, owner),
                )
            except sqlite3.IntegrityError as error:
                raise SkillConflict("Skill name already exists") from error
            count = db.execute(
                "SELECT count(*) FROM webui_skill_files WHERE skill_id=? AND owner=?",
                (sid, owner),
            ).fetchone()[0]
            return present_skill(
                owned_skill(db, owner, sid), detail=True, file_count=count
            )

    def delete(self, owner: str, sid: str) -> None:
        with self.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            owned_skill(db, owner, sid)
            db.execute(
                "DELETE FROM webui_skills WHERE id=? AND owner=?", (sid, owner)
            )
            states = skill_states(db, owner)
            if sid in states:
                del states[sid]
                _save_states(db, owner, states)

    def states(self, owner: str) -> dict:
        with self.database.read_snapshot() as db:
            return skill_states(db, owner)

    def update_states(self, owner: str, values: dict) -> dict:
        if (
            not isinstance(values, dict)
            or len(values) > 500
            or any(
                not isinstance(k, str)
                or not 1 <= len(k) <= 512
                or type(v) is not bool
                for k, v in values.items()
            )
        ):
            raise ValueError("Invalid Skill states")
        with self.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for identity in values:
                if identity.startswith("deployment:"):
                    validate_skill_name(identity.removeprefix("deployment:"))
                else:
                    owned_skill(db, owner, identity)
            _save_states(db, owner, values)
        return values
