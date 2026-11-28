"""Atomic occurrence claims and tool-free generation admission."""

import json
import uuid
from dataclasses import dataclass

from .content_filters import ContentFilterBlocked
from .database import SQLiteDatabase
from .generations import GenerationStore
from .schedule_cadence import next_run
from .schedules import _dependencies, _find, _iso
from .storage import _json, _now

_BATCH_LIMIT = 25
_MANUAL_COOLDOWN_US = 60_000_000


@dataclass(frozen=True)
class ScheduleOccurrence:
    id: str
    owner: str
    schedule_id: str
    run_id: str
    conversation_id: str


def _settle(db, row, status: str, error: str | None = None) -> None:
    if row["status"] not in {"claimed", "started"}:
        return
    now = _now()
    db.execute(
        "UPDATE webui_schedule_runs SET status=?,updated_at=? WHERE id=? AND owner=?",
        (status, now, row["id"], row["owner"]),
    )
    details = {
        "status": status,
        "conversationId": row["conversation_id"],
        "firedAt": _iso(row["due_at"]),
    }
    if error:
        details["error"] = error
    db.execute(
        "UPDATE webui_schedules SET run_count=run_count+1,failure_count=failure_count+?,last_run=?,updated_at=? WHERE id=? AND owner=?",
        (
            int(status != "success"),
            _json(details),
            now,
            row["schedule_id"],
            row["owner"],
        ),
    )


class ScheduleOccurrences:
    def __init__(self, database: SQLiteDatabase):
        self.database = database

    def claim_due(
        self,
        *,
        owner: str | None = None,
        schedule_id: str | None = None,
        manual: bool = False,
    ) -> list[ScheduleOccurrence]:
        if manual and (not owner or not schedule_id):
            raise ValueError("Manual claim requires owner and schedule")
        with self.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            now = _now()
            clauses = [
                "NOT EXISTS (SELECT 1 FROM webui_schedule_runs r WHERE r.schedule_id=s.id AND r.owner=s.owner AND r.status IN ('claimed','started'))"
            ]
            values = []
            if owner is not None:
                clauses.append("s.owner=?")
                values.append(owner)
            if schedule_id is not None:
                clauses.append("s.id=?")
                values.append(schedule_id)
            if manual:
                _find(db, owner, schedule_id)
                clauses.append(
                    "NOT EXISTS (SELECT 1 FROM webui_schedule_runs r WHERE r.schedule_id=s.id AND r.owner=s.owner AND r.created_at>?)"
                )
                values.append(now - _MANUAL_COOLDOWN_US)
            else:
                clauses.append("s.enabled=1 AND s.next_run<=?")
                values.append(now // 1000)
            rows = db.execute(
                "SELECT s.* FROM webui_schedules s WHERE "
                + " AND ".join(clauses)
                + " ORDER BY s.next_run,s.id LIMIT ?",
                (*values, _BATCH_LIMIT),
            ).fetchall()
            result = []
            for row in rows:
                due = now // 1000 if manual else row["next_run"]
                config = json.loads(row["config"])
                if not manual:
                    try:
                        following = next_run(
                            config["cadence"],
                            config["timezone"],
                            after_ms=due,
                            min_interval_minutes=0,
                        )
                    except KeyError, ValueError, TypeError:
                        following = None
                    db.execute(
                        "UPDATE webui_schedules SET next_run=?,updated_at=? WHERE id=? AND owner=?",
                        (following, now, row["id"], row["owner"]),
                    )
                occurrence_id = str(uuid.uuid4())
                inserted = db.execute(
                    "INSERT OR IGNORE INTO webui_schedule_runs(id,owner,schedule_id,due_at,status,created_at,updated_at) VALUES(?,?,?,?, 'claimed',?,?)",
                    (occurrence_id, row["owner"], row["id"], due, now, now),
                )
                if not inserted.rowcount:
                    continue
                # Admission and the occurrence binding commit together. No
                # worker can observe an unbound or duplicate generation.
                db.execute("SAVEPOINT schedule_admission")
                try:
                    _dependencies(db, row["owner"], config)
                    payload = {
                        "text": config["prompt"],
                        "agent_id": config["agent_id"],
                        "endpoint": "agents",
                        "clientRequestId": "schedule:" + occurrence_id,
                        "web_search": False,
                    }
                    if config.get("chatProjectId"):
                        payload["projectId"] = config["chatProjectId"]
                    if config.get("file_ids"):
                        payload["files"] = [
                            {"file_id": f} for f in config["file_ids"]
                        ]
                    generation, fresh = GenerationStore(
                        self.database
                    )._begin_locked(
                        db,
                        row["owner"],
                        payload,
                        schedule_occurrence_id=occurrence_id,
                    )
                    if not fresh:
                        raise RuntimeError("Unexpected occurrence id collision")
                    db.execute(
                        "UPDATE webui_schedule_runs SET run_id=?,conversation_id=? WHERE id=? AND owner=?",
                        (
                            generation.run_id,
                            generation.conversation_id,
                            occurrence_id,
                            row["owner"],
                        ),
                    )
                except (KeyError, ValueError, PermissionError) as error:
                    db.execute("ROLLBACK TO schedule_admission")
                    db.execute("RELEASE schedule_admission")
                    failed = db.execute(
                        "SELECT * FROM webui_schedule_runs WHERE id=?",
                        (occurrence_id,),
                    ).fetchone()
                    _settle(
                        db,
                        failed,
                        "error",
                        "content_filter"
                        if isinstance(error, ContentFilterBlocked)
                        else "schedule_admission_failed",
                    )
                    continue
                db.execute("RELEASE schedule_admission")
                result.append(
                    ScheduleOccurrence(
                        occurrence_id,
                        row["owner"],
                        row["id"],
                        generation.run_id,
                        generation.conversation_id,
                    )
                )
            return result

    def pending(self) -> list[ScheduleOccurrence]:
        """Return unexecuted admissions; actor acquisition will fence dispatch."""
        with self.database.read_snapshot() as db:
            rows = db.execute(
                "SELECT r.* FROM webui_schedule_runs r JOIN runs g ON g.id=r.run_id AND g.conversation_id=r.conversation_id "
                "JOIN conversations c ON c.id=g.conversation_id AND c.owner=r.owner "
                "WHERE r.status='claimed' AND g.status='running' AND "
                "json_extract(g.state,'$.scheduleOccurrenceId')=r.id ORDER BY r.created_at LIMIT ?",
                (_BATCH_LIMIT,),
            ).fetchall()
            return [
                ScheduleOccurrence(
                    r["id"],
                    r["owner"],
                    r["schedule_id"],
                    r["run_id"],
                    r["conversation_id"],
                )
                for r in rows
            ]

    def refresh(self) -> int:
        """Idempotently account for terminal generations, without replay."""
        with self.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            # The first actor claim durably changes the occurrence to
            # started. A lost or expired lease cannot be reacquired.
            abandoned = db.execute(
                "SELECT g.* FROM webui_schedule_runs r JOIN runs g ON g.id=r.run_id AND g.conversation_id=r.conversation_id "
                "JOIN conversations c ON c.id=g.conversation_id AND c.owner=r.owner "
                "WHERE r.status='started' AND g.status IN ('running','requires_action') "
                "AND json_extract(g.state,'$.scheduleOccurrenceId')=r.id "
                "AND NOT EXISTS(SELECT 1 FROM generation_actor_leases l WHERE l.run_id=g.id AND l.owner=r.owner AND l.expires_at>?) "
                "ORDER BY r.created_at LIMIT ?",
                (_now() // 1000, _BATCH_LIMIT),
            ).fetchall()
            for generation in abandoned:
                GenerationStore(self.database)._finish_locked(
                    db,
                    generation,
                    status="interrupted",
                    error="Scheduled generation interrupted",
                )
            rows = db.execute(
                "SELECT r.*,g.status AS generation_status FROM webui_schedule_runs r "
                "LEFT JOIN runs g ON g.id=r.run_id AND g.conversation_id=r.conversation_id "
                "AND json_extract(g.state,'$.scheduleOccurrenceId')=r.id "
                "LEFT JOIN conversations c ON c.id=g.conversation_id AND c.owner=r.owner "
                "WHERE r.status IN ('claimed','started') AND (c.id IS NULL OR g.status IN ('complete','error','aborted','interrupted')) "
                "ORDER BY r.created_at LIMIT ?",
                (_BATCH_LIMIT,),
            ).fetchall()
            for row in rows:
                status = {"complete": "success", "error": "error"}.get(
                    row["generation_status"], "interrupted"
                )
                _settle(db, row, status)
            return len(rows)
