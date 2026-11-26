"""Durable native creation intents adapted from final upstream v6.0.3.

The intent is committed before native execution. Native IDs are observed from
structured results or a read-only lookup; never guessed from names or listings.
Source: webui/durable_tasks.py (9b50208b63aac266387b4e3b6b8e9ce6128b17e4).
"""

import hashlib
import json
from collections.abc import Collection
from dataclasses import dataclass

from ..protocol.ids import new_session_id, new_task_id
from .database import SQLiteDatabase
from .generation_actor_lease import ActorLease, ActorLeaseLost
from .machine_permissions import authorize_machine_action, require_machine_scope
from .provider_common import ModelToolCall, complete_tool_batch
from .storage import WorkspaceRepository, _now


def _fingerprint(name: str, arguments: dict) -> str:
    return hashlib.sha256(
        json.dumps(
            [name, arguments],
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    ).hexdigest()


@dataclass(frozen=True)
class Intent:
    id: str
    name: str
    key: str
    machine: str
    state: str
    native_id: str | None


class DurableTaskRepository:
    """Private intent ledger; Control/Executor remain the native resource owners.

    A claimed creation is uncertain until the native identity is confirmed.
    Even a missing receipt does not authorize a second mutation.
    """

    def __init__(self, database: SQLiteDatabase):
        self.database = database

    @staticmethod
    def _intent(row) -> Intent:
        return Intent(
            row["id"],
            row["tool"],
            row["idempotency_key"],
            row["machine"],
            row["state"],
            row["native_id"],
        )

    def _run(self, db, lease: ActorLease):
        if lease.database.path != self.database.path:
            raise ActorLeaseLost("Generation actor database mismatch")
        lease.assert_current(db)
        row = db.execute(
            "SELECT r.*,c.owner FROM runs r JOIN conversations c ON c.id=r.conversation_id "
            "WHERE r.id=? AND c.owner=?",
            (lease.run_id, lease.owner),
        ).fetchone()
        if (
            row is None
            or db.execute(
                "SELECT 1 FROM agent_run_cancellations WHERE run_id=?",
                (lease.run_id,),
            ).fetchone()
        ):
            raise PermissionError("Generation was cancelled")
        return row

    def _authorize(
        self,
        db,
        run,
        machine: str,
        kind: str,
        scopes: set[str],
        expires: int | None,
        registered: Collection[str],
        authorization_id: str,
    ) -> None:
        agent = None
        if run["agent_id"] is not None:
            row = db.execute(
                "SELECT config FROM agents WHERE id=? AND owner=?",
                (run["agent_id"], run["owner"]),
            ).fetchone()
            if row is None:
                raise PermissionError("Generation Agent no longer exists")
            agent = json.loads(row[0])
        # Task is Control-owned metadata, but reserve it only for a selected
        # authorized Executor; no implicit Controller machine target.
        authorize_machine_action(
            run["owner"],
            authorization_id,
            machine,
            "read",
            agent=agent,
            scopes=scopes,
            expires=expires,
            repo=WorkspaceRepository(self.database),
            registered=registered,
        )
        if kind == "task":
            require_machine_scope(scopes, "write", expires=expires)

    @staticmethod
    def _owned(
        db,
        owner: str,
        conversation_id: str,
        kind: str,
        native_id: str,
        machine: str | None = None,
    ) -> bool:
        if kind != "task" and machine is None:
            return False
        return (
            db.execute(
                "SELECT 1 FROM agent_durable_tasks WHERE owner=? AND conversation_id=? "
                "AND kind=? AND native_id=? AND state='active' "
                "AND (? IS NULL OR machine=?) LIMIT 1",
                (owner, conversation_id, kind, native_id, machine, machine),
            ).fetchone()
            is not None
        )

    def reserve(
        self,
        lease: ActorLease,
        call: ModelToolCall,
        *,
        machine: str,
        scopes: set[str],
        expires: int | None,
        registered: Collection[str],
        authorization_conversation_id: str | None = None,
    ) -> Intent:
        """Reserve one stable identity for the immutable native creation request."""
        complete_tool_batch([call])
        arguments = call.arguments
        if call.name == "task" and arguments.get("action") == "create":
            kind = "task"
            if arguments.keys() - {"action", "label", "objective"}:
                raise ValueError("Invalid native Task creation")
        elif call.name == "session_start":
            kind = "session"
            if (
                arguments.keys()
                - {"executor_id", "task_id", "workdir", "label"}
                or arguments.get("executor_id") != machine
                or not isinstance(arguments.get("task_id"), str)
                or not arguments["task_id"]
            ):
                raise ValueError(
                    "Session reservation requires an explicit Executor and Task"
                )
        else:
            raise ValueError("Not a supported native creation")
        identity = _fingerprint(
            "native-intent",
            {
                "owner": lease.owner,
                "run": lease.run_id,
                "call": call.call_id,
            },
        )
        fingerprint = _fingerprint(call.name, arguments)
        with self.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            run = self._run(db, lease)
            conversation_id = run["conversation_id"]
            authorization_id = authorization_conversation_id or conversation_id
            if authorization_id != conversation_id:
                raise PermissionError(
                    "Inherited authorization requires a verified child lineage"
                )
            prior = db.execute(
                "SELECT * FROM agent_durable_tasks WHERE id=?", (identity,)
            ).fetchone()
            if prior is not None:
                if (
                    prior["fingerprint"] != fingerprint
                    or prior["machine"] != machine
                    or prior["agent_id"] != run["agent_id"]
                    or prior["authorization_conversation_id"]
                    != authorization_id
                ):
                    raise PermissionError(
                        "A replayed tool call cannot change a durable request"
                    )
                if prior["state"] not in {"reserved", "unknown", "active"}:
                    raise PermissionError("Durable task request was cancelled")
                scopes, expires = self._grant(prior, scopes, expires)
            self._authorize(
                db,
                run,
                machine,
                kind,
                scopes,
                expires,
                registered,
                authorization_id,
            )
            if kind == "session" and not self._owned(
                db, lease.owner, conversation_id, "task", arguments["task_id"]
            ):
                raise PermissionError(
                    "Native Task does not belong to this conversation"
                )
            if prior is not None:
                return self._intent(prior)
            key = new_task_id() if kind == "task" else new_session_id()
            now = _now()
            db.execute(
                "INSERT INTO agent_durable_tasks(id,owner,conversation_id,authorization_conversation_id,"
                "run_id,call_id,agent_id,tool,kind,machine,requirements,fingerprint,idempotency_key,key_hash,"
                "scopes,grant_expires,state,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    identity,
                    lease.owner,
                    conversation_id,
                    authorization_id,
                    lease.run_id,
                    call.call_id,
                    run["agent_id"],
                    call.name,
                    kind,
                    machine,
                    json.dumps({machine: "read"}),
                    fingerprint,
                    key,
                    hashlib.sha256(key.encode()).hexdigest(),
                    json.dumps(sorted(scopes)),
                    expires,
                    "reserved",
                    now,
                    now,
                ),
            )
            return self._intent(
                db.execute(
                    "SELECT * FROM agent_durable_tasks WHERE id=?", (identity,)
                ).fetchone()
            )

    @staticmethod
    def _grant(
        row, scopes: set[str], expires: int | None
    ) -> tuple[set[str], int | None]:
        deadlines = [
            value
            for value in (row["grant_expires"], expires)
            if value is not None
        ]
        return scopes.intersection(json.loads(row["scopes"])), min(
            deadlines
        ) if deadlines else None

    @staticmethod
    def _row(db, lease: ActorLease, intent: Intent):
        row = db.execute(
            "SELECT * FROM agent_durable_tasks WHERE id=? AND owner=? AND run_id=?",
            (intent.id, lease.owner, lease.run_id),
        ).fetchone()
        if (
            row is None
            or row["idempotency_key"] != intent.key
            or row["tool"] != intent.name
            or row["machine"] != intent.machine
        ):
            raise PermissionError(
                "Native intent does not belong to this generation"
            )
        return row

    def claim(
        self,
        lease: ActorLease,
        intent: Intent,
        call: ModelToolCall,
        *,
        scopes: set[str],
        expires: int | None,
        registered: Collection[str],
    ) -> bool:
        """Persist the single dispatch claim before crossing native admission."""
        complete_tool_batch([call])
        with self.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            run = self._run(db, lease)
            row = self._row(db, lease, intent)
            if row["call_id"] != call.call_id or row[
                "fingerprint"
            ] != _fingerprint(call.name, call.arguments):
                raise PermissionError(
                    "A replayed tool call cannot change a durable request"
                )
            if row["kind"] == "session" and not self._owned(
                db,
                lease.owner,
                run["conversation_id"],
                "task",
                call.arguments["task_id"],
            ):
                raise PermissionError(
                    "Native Task does not belong to this conversation"
                )
            scopes, expires = self._grant(row, scopes, expires)
            self._authorize(
                db,
                run,
                row["machine"],
                row["kind"],
                scopes,
                expires,
                registered,
                row["authorization_conversation_id"],
            )
            if row["state"] != "reserved":
                return False
            db.execute(
                "UPDATE agent_durable_tasks SET state='unknown',updated_at=? WHERE id=?",
                (_now(), intent.id),
            )
            return True

    def bind(self, lease: ActorLease, intent: Intent, native_id: str) -> Intent:
        """Record a trusted native response/lookup with the exact reserved ID."""
        with self.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if lease.database.path != self.database.path:
                raise ActorLeaseLost("Generation actor database mismatch")
            lease.assert_current(db)
            row = self._row(db, lease, intent)
            if (
                native_id != row["idempotency_key"]
                or row["native_id"] not in (None, native_id)
                or row["state"] not in {"unknown", "active", "cancel_pending"}
            ):
                raise PermissionError(
                    "Native creation evidence does not match its reservation"
                )
            db.execute(
                "UPDATE agent_durable_tasks SET native_id=?,state=CASE WHEN state='unknown' "
                "THEN 'active' ELSE state END,updated_at=? WHERE id=?",
                (native_id, _now(), intent.id),
            )
            return self._intent(
                db.execute(
                    "SELECT * FROM agent_durable_tasks WHERE id=?", (intent.id,)
                ).fetchone()
            )

    def owned(
        self,
        owner: str,
        conversation_id: str,
        kind: str,
        native_id: str,
        *,
        machine: str | None = None,
    ) -> bool:
        """Ownership evidence is separate from live native authorization."""
        with self.database.read_snapshot() as db:
            return self._owned(
                db, owner, conversation_id, kind, native_id, machine
            )

    def is_run_cancelled(self, run_id: str) -> bool:
        with self.database.connect() as db:
            return (
                db.execute(
                    "SELECT 1 FROM agent_run_cancellations WHERE run_id=?",
                    (run_id,),
                ).fetchone()
                is not None
            )

    def cancel_run(self, owner: str, run_id: str) -> None:
        """Persist explicit abort before stopping a generation or its native work."""
        with self.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT c.owner FROM runs r JOIN conversations c ON c.id=r.conversation_id "
                "WHERE r.id=?",
                (run_id,),
            ).fetchone()
            if row is None or row["owner"] != owner:
                raise PermissionError("Generation owner mismatch")
            now = _now()
            db.execute(
                "INSERT OR IGNORE INTO agent_run_cancellations(run_id,created_at) VALUES(?,?)",
                (run_id, now),
            )
            db.execute(
                "UPDATE agent_durable_tasks SET state=CASE WHEN state='reserved' "
                "THEN 'stopped' ELSE 'cancel_pending' END,updated_at=?,next_attempt_at=NULL "
                "WHERE owner=? AND run_id=? AND state IN ('reserved','active','unknown')",
                (now, owner, run_id),
            )

    def _revoked(self, db, row, registered: Collection[str]) -> bool:
        """Recheck original grant and current policy; interruption alone is not revocation."""
        owner = row["owner"]
        for conversation_id in (
            row["conversation_id"],
            row["authorization_conversation_id"],
        ):
            if (
                db.execute(
                    "SELECT 1 FROM conversations WHERE id=? AND owner=?",
                    (conversation_id, owner),
                ).fetchone()
                is None
            ):
                return True
        if (
            row["run_id"] is not None
            and db.execute(
                "SELECT 1 FROM agent_run_cancellations WHERE run_id=? "
                "UNION ALL SELECT 1 FROM runs WHERE id=? AND status='aborted'",
                (row["run_id"], row["run_id"]),
            ).fetchone()
        ):
            return True
        try:
            self._authorize(
                db,
                row,
                row["machine"],
                row["kind"],
                set(json.loads(row["scopes"])),
                row["grant_expires"],
                registered,
                row["authorization_conversation_id"],
            )
        except PermissionError, KeyError, ValueError:
            return True
        return False

    def cancellation_batch(
        self, registered: Collection[str], *, limit: int = 32
    ) -> list[dict]:
        """Latch revocation and select bounded due cleanup work in a single transaction."""
        if not 1 <= limit <= 64:
            raise ValueError("Invalid cancellation batch size")
        now = _now()
        pending = []
        with self.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                "SELECT * FROM agent_durable_tasks "
                "WHERE state IN ('reserved','unknown','active','cancel_pending') "
                "AND (next_attempt_at IS NULL OR next_attempt_at<=?) "
                "ORDER BY COALESCE(next_attempt_at,0),created_at,id LIMIT ?",
                (now, limit),
            ).fetchall()
            for row in rows:
                revoked = row["state"] == "cancel_pending" or self._revoked(
                    db, row, registered
                )
                if not revoked:
                    db.execute(
                        "UPDATE agent_durable_tasks SET next_attempt_at=? WHERE id=?",
                        (now + 5_000_000, row["id"]),
                    )
                    continue
                if row["state"] == "reserved":
                    # The single dispatch claim has never been issued.
                    db.execute(
                        "UPDATE agent_durable_tasks SET state='stopped',updated_at=?,next_attempt_at=NULL WHERE id=?",
                        (now, row["id"]),
                    )
                    continue
                db.execute(
                    "UPDATE agent_durable_tasks SET state='cancel_pending',updated_at=?,next_attempt_at=? WHERE id=?",
                    (now, now + 120_000_000, row["id"]),
                )
                pending.append(dict(row))
        return pending

    def finish_cancellation(self, identity: str, *, stopped: bool) -> None:
        """Only positive native evidence settles a claimed cancellation."""
        now = _now()
        with self.database.connect() as db:
            db.execute(
                "UPDATE agent_durable_tasks SET state=?,updated_at=?,next_attempt_at=?,last_error=?,"
                "native_id=CASE WHEN ? THEN idempotency_key ELSE native_id END "
                "WHERE id=? AND state='cancel_pending'",
                (
                    "stopped" if stopped else "cancel_pending",
                    now,
                    None if stopped else now + 5_000_000,
                    None if stopped else "Native cancellation unconfirmed",
                    stopped,
                    identity,
                ),
            )
