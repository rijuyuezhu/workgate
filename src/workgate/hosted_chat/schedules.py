"""Owner-scoped scheduled chat configuration, independent of dispatch."""

import json
import uuid

from .database import SQLiteDatabase
from .schedule_cadence import (
    MIN_INTERVAL_MINUTES,
    _zone,
    next_run,
    validate_cadence_interval,
)
from .storage import _json, _now, _stamp

SCHEDULE_LIMITS = {
    "maxPerUser": 25,
    "minIntervalMinutes": MIN_INTERVAL_MINUTES,
    "requireProject": False,
}
_FIELDS = frozenset(
    {
        "name",
        "prompt",
        "agent_id",
        "cadence",
        "timezone",
        "target",
        "file_ids",
        "chatProjectId",
    }
)
_REQUIRED = frozenset({"name", "prompt", "agent_id", "cadence", "timezone"})


class ScheduleConflict(ValueError):
    pass


def _iso(ms: int | None):
    return _stamp(ms / 1000) if ms is not None else None


def _validate(config: dict):
    if not config.keys() >= _REQUIRED or config.keys() - _FIELDS:
        raise ValueError("Invalid schedule fields")
    for key, limit in (("name", 256), ("prompt", 32000), ("agent_id", 128)):
        value = config[key]
        if (
            not isinstance(value, str)
            or not value.strip()
            or len(value) > limit
        ):
            raise ValueError("Invalid schedule " + key)
    if config.get("target", "new") != "new":
        raise ValueError("Only new-chat schedules are supported")
    if len(config["prompt"].encode()) > 128 * 1024:
        raise ValueError("Schedule prompt exceeds byte limit")
    files = config.get("file_ids", [])
    if (
        not isinstance(files, list)
        or len(files) > 10
        or any(not isinstance(f, str) or not 1 <= len(f) <= 128 for f in files)
        or len(set(files)) != len(files)
    ):
        raise ValueError("Invalid schedule files")
    project = config.get("chatProjectId")
    if project is not None and (
        not isinstance(project, str) or not 1 <= len(project) <= 128
    ):
        raise ValueError("Invalid schedule project")
    validate_cadence_interval(config["cadence"])
    _zone(config["timezone"])


def _dependencies(db, owner, config):
    if not db.execute(
        "SELECT 1 FROM agents WHERE id=? AND owner=?",
        (config["agent_id"], owner),
    ).fetchone():
        raise KeyError("Schedule Agent not found")
    project = config.get("chatProjectId")
    if (
        project is not None
        and not db.execute(
            "SELECT 1 FROM projects WHERE id=? AND owner=?", (project, owner)
        ).fetchone()
    ):
        raise KeyError("Schedule project not found")
    for file in config.get("file_ids", []):
        if not db.execute(
            "SELECT 1 FROM attachments WHERE id=? AND owner=? AND NOT EXISTS (SELECT 1 FROM attachment_deletions WHERE file_id=?)",
            (file, owner, file),
        ).fetchone():
            raise KeyError("Schedule attachment not found")


def _present(db, row):
    active = db.execute(
        "SELECT conversation_id FROM webui_schedule_runs WHERE owner=? AND schedule_id=? AND status='started' AND conversation_id IS NOT NULL",
        (row["owner"], row["id"]),
    ).fetchall()
    return {
        **json.loads(row["config"]),
        "id": row["id"],
        "user": row["owner"],
        "enabled": bool(row["enabled"]),
        "configRevision": row["revision"],
        "runCount": row["run_count"],
        "failureCount": row["failure_count"],
        "lastRun": json.loads(row["last_run"]) if row["last_run"] else None,
        "nextRunAt": _iso(row["next_run"]) if row["enabled"] else None,
        "inFlight": [
            {"conversationId": record["conversation_id"]} for record in active
        ],
        "createdAt": _stamp(row["created_at"] / 1_000_000),
        "updatedAt": _stamp(row["updated_at"] / 1_000_000),
    }


def _find(db, owner, sid):
    row = db.execute(
        "SELECT * FROM webui_schedules WHERE owner=? AND id=?", (owner, sid)
    ).fetchone()
    if row is None:
        raise KeyError("Schedule not found")
    return row


class ScheduleRepository:
    def __init__(self, database: SQLiteDatabase):
        self.database = database

    def list(self, owner: str) -> list[dict]:
        with self.database.read_snapshot() as db:
            return [
                _present(db, row)
                for row in db.execute(
                    "SELECT * FROM webui_schedules WHERE owner=? ORDER BY created_at DESC,id",
                    (owner,),
                )
            ]

    def get(self, owner: str, sid: str) -> dict:
        with self.database.read_snapshot() as db:
            return _present(db, _find(db, owner, sid))

    def create(self, owner: str, data: dict) -> dict:
        if not isinstance(data, dict) or set(data) - (
            _FIELDS | {"enabled", "clientRequestId"}
        ):
            raise ValueError("Invalid schedule request")
        request_id = data.get("clientRequestId")
        enabled = data.get("enabled", True)
        if (
            not isinstance(request_id, str)
            or not 1 <= len(request_id) <= 128
            or type(enabled) is not bool
        ):
            raise ValueError("Invalid schedule admission")
        config = {key: data[key] for key in _FIELDS if key in data}
        config.setdefault("target", "new")
        _validate(config)
        with self.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute(
                "SELECT * FROM webui_schedules WHERE owner=? AND request_id=?",
                (owner, request_id),
            ).fetchone()
            if prior is not None:
                return _present(db, prior)
            _dependencies(db, owner, config)
            count = db.execute(
                "SELECT COUNT(*) FROM webui_schedules WHERE owner=?", (owner,)
            ).fetchone()[0]
            if count >= SCHEDULE_LIMITS["maxPerUser"]:
                raise ValueError("Schedule limit reached")
            now = _now()
            upcoming = (
                next_run(
                    config["cadence"], config["timezone"], after_ms=now // 1000
                )
                if enabled
                else None
            )
            sid = str(uuid.uuid4())
            db.execute(
                "INSERT INTO webui_schedules(id,owner,request_id,config,enabled,next_run,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    sid,
                    owner,
                    request_id,
                    _json(config),
                    int(enabled),
                    upcoming,
                    now,
                    now,
                ),
            )
            return _present(db, _find(db, owner, sid))

    def update(self, owner: str, sid: str, data: dict) -> dict:
        if not isinstance(data, dict) or set(data) - (
            _FIELDS | {"enabled", "expectedConfigRevision"}
        ):
            raise ValueError("Invalid schedule patch")
        with self.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = _find(db, owner, sid)
            expected = data.get("expectedConfigRevision", row["revision"])
            if type(expected) is not int or expected != row["revision"]:
                raise ScheduleConflict("Schedule configuration changed")
            enabled = data.get("enabled", bool(row["enabled"]))
            if type(enabled) is not bool:
                raise ValueError("enabled must be a boolean")
            previous = json.loads(row["config"])
            config = {
                **previous,
                **{key: data[key] for key in _FIELDS if key in data},
            }
            _validate(config)
            if enabled:
                _dependencies(db, owner, config)
            timing_changed = any(
                config[key] != previous[key] for key in ("cadence", "timezone")
            )
            upcoming = row["next_run"] if enabled else None
            now = _now()
            if enabled and (timing_changed or not row["enabled"]):
                upcoming = next_run(
                    config["cadence"], config["timezone"], after_ms=now // 1000
                )
            db.execute(
                "UPDATE webui_schedules SET config=?,enabled=?,next_run=?,revision=revision+1,updated_at=? WHERE id=? AND owner=?",
                (_json(config), int(enabled), upcoming, now, sid, owner),
            )
            return _present(db, _find(db, owner, sid))

    def delete(self, owner: str, sid: str) -> None:
        with self.database.connect() as db:
            if not db.execute(
                "DELETE FROM webui_schedules WHERE owner=? AND id=?",
                (owner, sid),
            ).rowcount:
                raise KeyError("Schedule not found")
