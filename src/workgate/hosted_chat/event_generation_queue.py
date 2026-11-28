"""Atomic event admission and terminal reconciliation, without actor replay."""

import json
from dataclasses import dataclass

from .database import SQLiteDatabase
from .event_actor_identity import live_event
from .event_delivery_queue import _batch_input
from .event_source_keys import _now
from .generations import GenerationConflict, GenerationStore

_BATCH_LIMIT = 8


@dataclass(frozen=True)
class EventGeneration:
    delivery_id: str
    owner: str
    run_id: str


def _settle(db, row, status, *, result=None, error=None):
    db.execute(
        "UPDATE agent_event_deliveries SET status=?,result=?,error_code=?,error_type=?,"
        "lease_owner=NULL,lease_until=NULL,updated_at=? WHERE owner=? AND source_key_id=? "
        "AND (id=? OR batch_root_id=?) AND status IN ('pending','leased')",
        (
            status,
            json.dumps(result) if result else None,
            error,
            error,
            _now(),
            row["owner"],
            row["source_key_id"],
            row["id"],
            row["id"],
        ),
    )


class EventGenerations:
    def __init__(self, database: SQLiteDatabase):
        self.database = database

    def admit(self) -> list[EventGeneration]:
        """Admit the first eligible delivery in each lane, in one transaction."""
        with self.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                "SELECT d.* FROM agent_event_deliveries d WHERE d.status='pending' "
                "AND d.batch_root_id IS NULL AND d.mode IN ('fire','continue') AND d.available_at<=? "
                "AND NOT EXISTS(SELECT 1 FROM agent_event_deliveries p WHERE p.owner=d.owner "
                "AND p.ordering_key=d.ordering_key AND p.batch_root_id IS NULL AND p.seq<d.seq "
                "AND p.status IN ('pending','leased')) ORDER BY d.seq LIMIT ?",
                (_now(), _BATCH_LIMIT),
            ).fetchall()
            result = []
            for row in rows:
                owner, delivery = row["owner"], row["id"]
                if live_event(db, owner, delivery) is None:
                    _settle(db, row, "dead", error="event_authorization_lost")
                    continue
                if row["run_id"] is not None:
                    result.append(
                        EventGeneration(delivery, owner, row["run_id"])
                    )
                    continue
                envelope = json.loads(row["envelope"])
                target = envelope["target"]
                members = db.execute(
                    "SELECT * FROM agent_event_deliveries WHERE batch_root_id=? AND owner=? "
                    "AND source_key_id=? AND status='pending' ORDER BY seq",
                    (delivery, owner, row["source_key_id"]),
                ).fetchall()
                prompt = (
                    _batch_input(row, members)
                    if row["coalesce_key"]
                    else envelope["input"]
                )
                payload = {
                    "text": prompt,
                    "agent_id": target["agentId"],
                    "endpoint": "agents",
                    "clientRequestId": delivery,
                    "web_search": False,
                }
                if row["mode"] == "continue":
                    payload["conversationId"] = target["conversationId"]
                db.execute("SAVEPOINT event_admission")
                try:
                    generation, fresh = GenerationStore(
                        self.database
                    )._begin_locked(
                        db,
                        owner,
                        payload,
                        event_delivery_id=delivery,
                    )
                    if not fresh:
                        raise ValueError("Unexpected event admission collision")
                    db.execute(
                        "UPDATE agent_event_deliveries SET run_id=?,updated_at=? WHERE id=? AND owner=?",
                        (generation.run_id, _now(), delivery, owner),
                    )
                except (
                    KeyError,
                    ValueError,
                    PermissionError,
                    GenerationConflict,
                ):
                    db.execute("ROLLBACK TO event_admission")
                    db.execute("RELEASE event_admission")
                    _settle(db, row, "dead", error="event_admission_failed")
                    continue
                db.execute("RELEASE event_admission")
                result.append(
                    EventGeneration(delivery, owner, generation.run_id)
                )
            return result

    def refresh(self) -> int:
        """Settle completed or abandoned actors; never replay a started delivery."""
        with self.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                "SELECT d.* FROM agent_event_deliveries d WHERE d.batch_root_id IS NULL "
                "AND d.mode IN ('fire','continue') AND (d.status IN ('pending','leased') "
                "OR (d.status='dead' AND EXISTS(SELECT 1 FROM runs g WHERE g.id=d.run_id "
                "AND g.status IN ('running','requires_action')))) ORDER BY d.seq",
            ).fetchall()
            count = 0
            for row in rows:
                run = db.execute(
                    "SELECT r.* FROM runs r JOIN conversations c ON c.id=r.conversation_id "
                    "WHERE r.id=? AND c.owner=? AND json_extract(r.state,'$.eventDeliveryId')=?",
                    (row["run_id"], row["owner"], row["id"]),
                ).fetchone()
                valid = live_event(db, row["owner"], row["id"]) is not None
                if run is not None and run["status"] == "complete":
                    _settle(
                        db,
                        row,
                        "succeeded",
                        result={
                            "runId": run["id"],
                            "conversationId": run["conversation_id"],
                        },
                    )
                elif run is not None and run["status"] in {
                    "error",
                    "aborted",
                    "interrupted",
                }:
                    _settle(db, row, "dead", error="event_generation_failed")
                else:
                    expired = (
                        row["status"] == "leased"
                        and not db.execute(
                            "SELECT 1 FROM generation_actor_leases WHERE run_id=? AND owner=? AND token=? AND expires_at>?",
                            (
                                row["run_id"],
                                row["owner"],
                                row["lease_owner"],
                                _now(),
                            ),
                        ).fetchone()
                    )
                    if valid and not expired and row["status"] != "dead":
                        continue
                    if run is not None:
                        GenerationStore(self.database)._finish_locked(
                            db,
                            run,
                            status="interrupted",
                            error="Event generation interrupted",
                        )
                    _settle(
                        db, row, "dead", error="event_generation_interrupted"
                    )
                count += 1
                if count >= _BATCH_LIMIT:
                    break
            return count
