"""SQLite CAS ownership for ordinary and private detached generation actors.

Only one Controller may generate or invoke tools for an owned run at a time.
All lease identity comes from the server's persistent run, never client input.
Recovery requires a newly authenticated request; a startup task never silently
inherits machine permissions from a dead process.
"""

import asyncio
import secrets
import sqlite3
import time
from dataclasses import dataclass

from .database import SQLiteDatabase

_LEASE_MS = 120_000
_RENEW_S = 20


def _now() -> int:
    return int(time.time() * 1000)


class ActorLeaseLost(RuntimeError):
    """The current actor no longer owns a live generation."""


@dataclass(frozen=True)
class ActorLease:
    database: SQLiteDatabase
    owner: str
    run_id: str
    token: str
    detached_task_id: str | None = None
    wakeup_task_id: str | None = None

    @classmethod
    def claim(
        cls, database: SQLiteDatabase, owner: str, run_id: str
    ) -> ActorLease | None:
        token = secrets.token_hex(24)
        with database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            now = _now()
            match = db.execute(
                "SELECT 1 FROM runs r JOIN conversations c ON c.id=r.conversation_id "
                "WHERE r.id=? AND c.owner=? AND r.status='running' "
                "AND NOT EXISTS(SELECT 1 FROM active_subagent_threads a WHERE a.owner=c.owner AND a.thread_id=c.id) "
                "AND json_extract(r.state,'$.completionWakeupTaskId') IS NULL",
                (run_id, owner),
            ).fetchone()
            if match is None:
                return None
            db.execute(
                "INSERT INTO generation_actor_leases(run_id,owner,token,expires_at) "
                "VALUES(?,?,?,?) ON CONFLICT(run_id) DO UPDATE SET "
                "owner=excluded.owner,token=excluded.token,expires_at=excluded.expires_at "
                "WHERE generation_actor_leases.expires_at<=?",
                (run_id, owner, token, now + _LEASE_MS, now),
            )
            row = db.execute(
                "SELECT token FROM generation_actor_leases WHERE run_id=? AND owner=?",
                (run_id, owner),
            ).fetchone()
            return (
                cls(database, owner, run_id, token)
                if row and row["token"] == token
                else None
            )

    def is_current(self, db: sqlite3.Connection | None = None) -> bool:
        if db is None:
            with self.database.connect() as connection:
                return self.is_current(connection)
        if self.detached_task_id is not None:
            return (
                db.execute(
                    "SELECT 1 FROM subagent_tasks t JOIN runs r ON r.id=t.child_run_id "
                    "JOIN conversations c ON c.id=r.conversation_id JOIN active_subagent_threads a "
                    "ON a.owner=t.owner AND a.thread_id=c.id AND a.parent_run_id=t.parent_run_id "
                    "WHERE t.id=? AND t.owner=? AND r.id=? AND c.owner=t.owner "
                    "AND c.id=t.child_conversation_id AND a.active=1 AND t.child_registered=1 "
                    "AND t.status='running' AND r.status='running' AND t.cancel_requested=0 "
                    "AND json_extract(r.state,'$.detachedTaskId')=t.id "
                    "AND t.lease_owner=? AND t.lease_until>? "
                    "AND (t.grant_expires IS NULL OR t.grant_expires>?)",
                    (
                        self.detached_task_id,
                        self.owner,
                        self.run_id,
                        self.token,
                        _now(),
                        time.time(),
                    ),
                ).fetchone()
                is not None
            )
        if self.wakeup_task_id is not None:
            return (
                db.execute(
                    "SELECT 1 FROM subagent_wakeups w JOIN subagent_tasks t ON t.id=w.task_id "
                    "JOIN runs r ON r.id=w.run_id JOIN conversations c ON c.id=r.conversation_id "
                    "WHERE w.task_id=? AND w.owner=? AND w.run_id=? AND c.owner=w.owner "
                    "AND c.id=w.parent_conversation_id AND t.owner=w.owner "
                    "AND t.result_claim_key='wakeup:'||w.task_id AND t.result_claimed=1 "
                    "AND json_extract(r.state,'$.completionWakeupTaskId')=w.task_id "
                    "AND w.status='dispatched' AND r.status='running' AND w.lease_owner=? "
                    "AND w.lease_until>? AND (t.grant_expires IS NULL OR t.grant_expires>?)",
                    (
                        self.wakeup_task_id,
                        self.owner,
                        self.run_id,
                        self.token,
                        _now(),
                        time.time(),
                    ),
                ).fetchone()
                is not None
            )
        return (
            db.execute(
                "SELECT 1 FROM generation_actor_leases l JOIN runs r ON r.id=l.run_id "
                "JOIN conversations c ON c.id=r.conversation_id WHERE l.run_id=? AND l.owner=? "
                "AND l.token=? AND l.expires_at>? AND r.status='running' AND c.owner=l.owner "
                "AND NOT EXISTS(SELECT 1 FROM active_subagent_threads a WHERE a.owner=c.owner AND a.thread_id=c.id) "
                "AND json_extract(r.state,'$.completionWakeupTaskId') IS NULL",
                (self.run_id, self.owner, self.token, _now()),
            ).fetchone()
            is not None
        )

    def assert_current(self, db: sqlite3.Connection | None = None) -> None:
        """For mutations, check inside the same immediate write transaction."""
        if not self.is_current(db):
            raise ActorLeaseLost(
                "Generation actor lease expired or was replaced"
            )

    def renew(self) -> bool:
        if self.wakeup_task_id is not None:
            raise ActorLeaseLost("Wakeup actor must renew its dispatch lease")
        if self.detached_task_id is not None:
            raise ActorLeaseLost("Detached actor must renew its task lease")
        with self.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            now = _now()
            cursor = db.execute(
                "UPDATE generation_actor_leases SET expires_at=? WHERE run_id=? AND owner=? "
                "AND token=? AND expires_at>? AND EXISTS(SELECT 1 FROM runs r "
                "JOIN conversations c ON c.id=r.conversation_id "
                "WHERE r.id=generation_actor_leases.run_id AND r.status='running' "
                "AND c.owner=generation_actor_leases.owner)",
                (now + _LEASE_MS, self.run_id, self.owner, self.token, now),
            )
            return cursor.rowcount == 1

    def release(self) -> None:
        if self.wakeup_task_id is not None:
            raise ActorLeaseLost("Wakeup actor must release its dispatch lease")
        if self.detached_task_id is not None:
            raise ActorLeaseLost("Detached actor must release its task lease")
        with self.database.connect() as db:
            db.execute(
                "DELETE FROM generation_actor_leases WHERE run_id=? AND owner=? AND token=?",
                (self.run_id, self.owner, self.token),
            )

    async def keep_alive(self) -> None:
        while True:
            await asyncio.sleep(_RENEW_S)
            if not await asyncio.to_thread(self.renew):
                return


def active_run_ids(database: SQLiteDatabase) -> set[str]:
    """Foreground actors; detached execution is owned by its task lease."""
    with database.connect() as db:
        return {
            row["run_id"]
            for row in db.execute(
                "SELECT l.run_id FROM generation_actor_leases l JOIN runs r ON r.id=l.run_id "
                "JOIN conversations c ON c.id=r.conversation_id "
                "WHERE l.expires_at>? AND r.status='running' AND c.owner=l.owner",
                (_now(),),
            )
        }
