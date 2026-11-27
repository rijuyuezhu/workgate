"""Detached reservation lease and settlement; not an execution grant.

An authenticated host must supply its live grant at claim, renewal and every
execution boundary. A persisted task grant cannot authorize a new worker.
"""

import json
import secrets
import sqlite3
import time
from dataclasses import dataclass

from .database import SQLiteDatabase
from .detached_subagents import _MAX_THREAD_HISTORY
from .generation_actor_lease import ActorLeaseLost
from .subagent_scope import SubagentScope
from .subagents import filter_subagent_result

_LEASE_MS = 30_000
_MAX_RESULT = 100_000


def _now() -> int:
    return int(time.time() * 1000)


def _task(db: sqlite3.Connection, owner: str, conversation: str, task_id: str):
    row = db.execute(
        "SELECT t.* FROM subagent_tasks t JOIN conversations c ON c.id=t.parent_conversation_id "
        "WHERE t.id=? AND t.owner=? AND t.parent_conversation_id=? AND c.owner=t.owner",
        (task_id, owner, conversation),
    ).fetchone()
    if row is None:
        raise PermissionError(
            "Detached task is outside the current owner scope"
        )
    return row


def _authorize(
    db,
    row,
    scopes: set[str],
    expires: int | None,
    disclosure_access: dict | None,
):
    options = json.loads(row["execution_options"])
    scope = SubagentScope.from_record(options["scope"])
    if (
        not scope.detached
        or scope.checkpoint_namespace != row["idempotency_key"]
        or scope.invocation_hash != row["fingerprint"]
        or scope.run_id != row["parent_run_id"]
        or list(scope.scopes) != json.loads(row["granted_scopes"])
        or scope.grant_expires != row["grant_expires"]
        or options.get("disclosure_access") != disclosure_access
        or not row["child_registered"]
    ):
        raise PermissionError("Detached task authority changed")
    if (
        db.execute(
            "SELECT 1 FROM active_subagent_threads WHERE owner=? AND thread_id=? AND parent_run_id=? AND active=1",
            (row["owner"], row["child_conversation_id"], row["parent_run_id"]),
        ).fetchone()
        is None
    ):
        raise PermissionError("Detached child registration is no longer active")
    scope.resolve_owned(db, row["owner"], row["parent_run_id"])
    scope.effective_grant(scopes, expires)


def _settle(
    db,
    row,
    status: str,
    *,
    result: str | None = None,
    history: str | None = None,
    error: str | None = None,
):
    # Child generation creation and atomic terminal projection are a separate
    # integration unit. Do not settle an existing execution without them.
    if row["child_run_id"] is not None:
        raise PermissionError(
            "Detached child generation requires runtime settlement"
        )
    now = _now()
    if history is not None:
        db.execute(
            "UPDATE subagent_threads SET messages=?,updated_at=? WHERE id=? AND owner=?",
            (history, now, row["thread_id"], row["owner"]),
        )
    db.execute(
        "UPDATE subagent_tasks SET status=?,result=?,error=?,updated_at=?,lease_owner=NULL,lease_until=NULL WHERE id=?",
        (status, result, error, now, row["id"]),
    )
    db.execute(
        "UPDATE active_subagent_threads SET active=0,updated_at=unixepoch() WHERE owner=? AND thread_id=? AND parent_run_id=?",
        (row["owner"], row["child_conversation_id"], row["parent_run_id"]),
    )


@dataclass(frozen=True)
class DetachedTaskLease:
    database: SQLiteDatabase
    owner: str
    conversation: str
    task_id: str
    token: str

    @classmethod
    def claim(
        cls,
        database: SQLiteDatabase,
        owner: str,
        conversation: str,
        task_id: str,
        *,
        scopes: set[str],
        expires: int | None,
        disclosure_access: dict | None = None,
    ) -> DetachedTaskLease | None:
        with database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = _task(db, owner, conversation, task_id)
            now = _now()
            if (
                row["status"] != "running"
                or row["cancel_requested"]
                or (row["lease_until"] is not None and row["lease_until"] > now)
            ):
                return None
            _authorize(db, row, scopes, expires, disclosure_access)
            token = secrets.token_hex(24)
            db.execute(
                "UPDATE subagent_tasks SET lease_owner=?,lease_until=? WHERE id=?",
                (token, now + _LEASE_MS, task_id),
            )
            return cls(database, owner, conversation, task_id, token)

    def _current(self, db):
        row = _task(db, self.owner, self.conversation, self.task_id)
        if (
            row["status"] != "running"
            or row["lease_owner"] != self.token
            or row["lease_until"] is None
            or row["lease_until"] <= _now()
        ):
            raise ActorLeaseLost("Detached task lease expired or was replaced")
        return row

    def assert_current(
        self,
        *,
        scopes: set[str],
        expires: int | None,
        disclosure_access: dict | None = None,
        db: sqlite3.Connection | None = None,
    ) -> None:
        if db is None:
            with self.database.read_snapshot() as snapshot:
                self.assert_current(
                    scopes=scopes,
                    expires=expires,
                    disclosure_access=disclosure_access,
                    db=snapshot,
                )
            return
        row = self._current(db)
        if row["cancel_requested"]:
            raise ActorLeaseLost("Detached task cancellation requested")
        _authorize(db, row, scopes, expires, disclosure_access)

    def renew(
        self,
        *,
        scopes: set[str],
        expires: int | None,
        disclosure_access: dict | None = None,
    ) -> None:
        with self.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self.assert_current(
                scopes=scopes,
                expires=expires,
                disclosure_access=disclosure_access,
                db=db,
            )
            db.execute(
                "UPDATE subagent_tasks SET lease_until=? WHERE id=?",
                (_now() + _LEASE_MS, self.task_id),
            )

    def release(self) -> None:
        with self.database.connect() as db:
            db.execute(
                "UPDATE subagent_tasks SET lease_owner=NULL,lease_until=NULL WHERE id=? AND owner=? AND parent_conversation_id=? AND lease_owner=?",
                (self.task_id, self.owner, self.conversation, self.token),
            )

    def complete(
        self,
        messages: list[dict],
        *,
        scopes: set[str],
        expires: int | None,
        disclosure_access: dict | None = None,
    ) -> None:
        with self.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self.assert_current(
                scopes=scopes,
                expires=expires,
                disclosure_access=disclosure_access,
                db=db,
            )
            row = self._current(db)
            initial = json.loads(row["initial_messages"])
            if messages[: len(initial)] != initial:
                raise ValueError(
                    "Detached transcript does not preserve its admitted input"
                )
            encoded = json.dumps(messages, ensure_ascii=False, allow_nan=False)
            if len(encoded.encode()) > _MAX_THREAD_HISTORY:
                raise ValueError(
                    "Subagent transcript exceeds durable thread limit"
                )
            result = filter_subagent_result(messages[len(initial) :])
            if len(result) > _MAX_RESULT:
                result = (
                    result[: _MAX_RESULT - len("\n[truncated]")]
                    + "\n[truncated]"
                )
            _settle(db, row, "completed", result=result, history=encoded)

    def fail(self) -> None:
        """Settle a failed worker without persisting provider error details."""
        with self.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._current(db)
            if row["cancel_requested"]:
                _settle(db, row, "cancelled")
            else:
                _settle(db, row, "error", error="Detached subagent task failed")

    def acknowledge_cancel(self) -> None:
        with self.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._current(db)
            if not row["cancel_requested"]:
                raise PermissionError(
                    "Detached task cancellation was not requested"
                )
            _settle(db, row, "cancelled")


def request_cancel(
    database: SQLiteDatabase, owner: str, conversation: str, task_id: str
) -> None:
    """An owner can revoke a reservation even after its grant has expired."""
    with database.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = _task(db, owner, conversation, task_id)
        if row["status"] != "running":
            return
        db.execute(
            "UPDATE subagent_tasks SET cancel_requested=1,updated_at=? WHERE id=?",
            (_now(), task_id),
        )
        if row["lease_until"] is None or row["lease_until"] <= _now():
            _settle(db, row, "cancelled")


def reconcile_reservations(
    database: SQLiteDatabase, owner: str, conversation: str
) -> int:
    """Retire idle cancelled/expired reservations, never launch a worker."""
    with database.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        rows = db.execute(
            "SELECT t.* FROM subagent_tasks t JOIN conversations c ON c.id=t.parent_conversation_id "
            "WHERE t.owner=? AND t.parent_conversation_id=? AND c.owner=t.owner AND t.status='running' "
            "AND t.child_run_id IS NULL AND (t.lease_until IS NULL OR t.lease_until<=?) "
            "AND (t.cancel_requested=1 OR t.grant_expires<=?) ORDER BY t.rowid LIMIT 100",
            (owner, conversation, _now(), time.time()),
        ).fetchall()
        for row in rows:
            if row["cancel_requested"]:
                _settle(db, row, "cancelled")
            else:
                _settle(db, row, "error", error="Authentication grant expired")
        return len(rows)
