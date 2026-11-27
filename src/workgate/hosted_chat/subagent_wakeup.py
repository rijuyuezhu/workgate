"""Internal child-completion envelope, adapted from pinned subagent_wakeup.py.

Registration shares the child reservation transaction. An envelope is routing
metadata, never an authorization grant or a public event submission.
"""

import json
import secrets
import sqlite3
import time
import uuid
from dataclasses import dataclass

from .database import SQLiteDatabase
from .generation_actor_lease import ActorLease
from .generations import Generation, GenerationConflict, GenerationStore
from .subagent_scope import SubagentScope

_LEASE_MS = 120_000
_COMPLETION_SETTING = "backgroundTasks.completionWakeups"


def register_wakeup(
    db: sqlite3.Connection, task_id: str, scope: SubagentScope
) -> None:
    """Prepare a completion intent for a real root parent reply."""
    if not scope.detached:
        raise PermissionError("Completion wakeup requires detached admission")
    if scope.parent_scope is not None:
        # Private nested/member edges have no independent public reply.
        return
    row = db.execute(
        "SELECT t.*,r.state AS parent_state,r.agent_id AS root_agent "
        "FROM subagent_tasks t JOIN runs r ON r.id=t.parent_run_id "
        "JOIN conversations c ON c.id=r.conversation_id "
        "WHERE t.id=? AND t.parent_run_id=? AND t.parent_conversation_id=c.id "
        "AND t.owner=c.owner AND t.idempotency_key=? AND t.fingerprint=? "
        "AND t.status='running' AND t.child_registered=1",
        (
            task_id,
            scope.run_id,
            scope.checkpoint_namespace,
            scope.invocation_hash,
        ),
    ).fetchone()
    if (
        row is None
        or (row["root_agent"] or "default") != row["parent_agent_id"]
    ):
        raise PermissionError("Completion wakeup parent lineage changed")
    setting = db.execute(
        "SELECT value FROM settings WHERE owner=? AND key=?",
        (row["owner"], _COMPLETION_SETTING),
    ).fetchone()
    if setting is not None and json.loads(setting["value"]) is False:
        return
    response_id = json.loads(row["parent_state"]).get("responseMessageId")
    if not isinstance(response_id, str) or not response_id:
        return
    created_at = row["created_at"]
    envelope = {
        "version": 1,
        "mode": "continue",
        "requestId": str(uuid.uuid4()),
        "deliveryId": task_id,
        "receivedAt": created_at,
        "principal": {"userId": row["owner"]},
        "event": {
            "id": task_id,
            "type": "subagent.completion",
            "occurredAt": created_at,
            "source": {"id": "subagent-completion", "type": "internal"},
            "payload": {
                "taskId": task_id,
                "threadId": row["thread_id"],
                "subagentType": row["subagent_type"],
            },
        },
        "target": {
            "agentId": row["parent_agent_id"],
            "conversationId": row["parent_conversation_id"],
            "parentMessageId": response_id,
        },
        "input": "A detached subagent task is waiting to complete.",
    }
    db.execute(
        "INSERT INTO subagent_wakeups(task_id,owner,parent_conversation_id,parent_run_id,"
        "parent_agent_id,envelope,status,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?,'pending',?,?)",
        (
            task_id,
            row["owner"],
            row["parent_conversation_id"],
            row["parent_run_id"],
            row["parent_agent_id"],
            json.dumps(
                envelope, ensure_ascii=False, sort_keys=True, allow_nan=False
            ),
            created_at,
            created_at,
        ),
    )


def _now() -> int:
    return int(time.time() * 1000)


def _row(db, owner: str, task_id: str):
    return db.execute(
        "SELECT w.*,t.status AS child_status,t.result,t.error AS child_error,"
        "t.result_claimed,t.result_claim_key,t.child_run_id,t.child_conversation_id,"
        "t.execution_options,t.granted_scopes,t.grant_expires,t.idempotency_key,"
        "t.fingerprint,t.thread_id,t.subagent_type "
        "FROM subagent_wakeups w JOIN subagent_tasks t ON t.id=w.task_id "
        "JOIN conversations c ON c.id=w.parent_conversation_id "
        "WHERE w.owner=? AND w.task_id=? AND t.owner=w.owner AND c.owner=w.owner "
        "AND t.parent_conversation_id=w.parent_conversation_id "
        "AND t.parent_run_id=w.parent_run_id AND t.parent_agent_id=w.parent_agent_id",
        (owner, task_id),
    ).fetchone()


def _retire(db, task_id: str, reason: str) -> None:
    db.execute(
        "UPDATE subagent_wakeups SET status='retired',error=?,updated_at=? WHERE task_id=? AND status='pending'",
        (reason, _now(), task_id),
    )


def _source(
    db,
    row,
    *,
    scopes: set[str],
    expires: int | None,
    disclosure_access: dict | None,
):
    """Validate immutable admission and live authority, then resolve the actual parent."""
    options = json.loads(row["execution_options"])
    scope = SubagentScope.from_record(options["scope"])
    if (
        not scope.detached
        or scope.parent_scope is not None
        or scope.run_id != row["parent_run_id"]
        or scope.checkpoint_namespace != row["idempotency_key"]
        or scope.invocation_hash != row["fingerprint"]
        or list(scope.scopes) != json.loads(row["granted_scopes"])
        or scope.grant_expires != row["grant_expires"]
        or options.get("disclosure_access") != disclosure_access
    ):
        raise PermissionError("Completion wakeup authority changed")
    scope.resolve_owned(db, row["owner"], row["parent_run_id"])
    scope.effective_grant(scopes, expires)
    parent = db.execute(
        "SELECT r.* FROM runs r JOIN conversations c ON c.id=r.conversation_id "
        "WHERE r.id=? AND c.owner=? AND c.id=?",
        (row["parent_run_id"], row["owner"], row["parent_conversation_id"]),
    ).fetchone()
    envelope = json.loads(row["envelope"])
    parent_state = json.loads(parent["state"])
    anchor = parent_state.get("responseMessageId")
    if (
        (parent["agent_id"] or "default") != row["parent_agent_id"]
        or envelope.get("principal") != {"userId": row["owner"]}
        or envelope.get("deliveryId") != row["task_id"]
        or envelope.get("target")
        != {
            "agentId": row["parent_agent_id"],
            "conversationId": row["parent_conversation_id"],
            "parentMessageId": anchor,
        }
    ):
        raise PermissionError("Completion wakeup parent identity changed")
    return parent, parent_state, anchor


def _latest_branch(db, conversation: str, anchor: str) -> str | None:
    """Find the newest persisted assistant descendant of the original reply."""
    rows = db.execute(
        "SELECT id,parent_id,role,created_at FROM messages WHERE conversation_id=? ORDER BY created_at DESC,id DESC",
        (conversation,),
    ).fetchall()
    messages = {row["id"]: row for row in rows}
    if anchor not in messages or messages[anchor]["role"] != "assistant":
        return None
    for row in rows:
        if row["role"] != "assistant":
            continue
        current = row
        seen = set()
        while current is not None and current["id"] not in seen:
            if current["id"] == anchor:
                return row["id"]
            seen.add(current["id"])
            current = messages.get(current["parent_id"])
    return None


def _child_is_owned(db, row) -> bool:
    child = db.execute(
        "SELECT r.state,r.status FROM runs r JOIN conversations c ON c.id=r.conversation_id "
        "WHERE r.id=? AND c.id=? AND c.owner=?",
        (row["child_run_id"], row["child_conversation_id"], row["owner"]),
    ).fetchone()
    return child is not None and (
        json.loads(child["state"]).get("detachedTaskId") == row["task_id"]
        and child["status"]
        == {"completed": "complete", "error": "error"}.get(row["child_status"])
    )


def _prompt(row) -> str:
    result = (
        row["result"]
        if row["child_status"] == "completed"
        else row["child_error"]
    )
    bounded = (
        (result or "").encode("utf-8")[: 48 * 1024].decode("utf-8", "ignore")
    )
    return (
        "A detached subagent task has settled. Continue the parent task using its durable result below.\n"
        + json.dumps(
            {
                "background_task_id": row["task_id"],
                "subagent_thread_id": row["thread_id"],
                "subagent_type": row["subagent_type"],
                "status": row["child_status"],
                "result": bounded,
            },
            ensure_ascii=False,
            allow_nan=False,
        )
    )


@dataclass(frozen=True)
class WakeupLease:
    """One authenticated continuation actor; persisted metadata is not a grant."""

    database: SQLiteDatabase
    owner: str
    task_id: str
    run_id: str
    token: str

    @property
    def actor(self) -> ActorLease:
        return ActorLease(
            self.database,
            self.owner,
            self.run_id,
            self.token,
            wakeup_task_id=self.task_id,
        )

    @classmethod
    def claim(
        cls,
        database: SQLiteDatabase,
        owner: str,
        task_id: str,
        *,
        scopes: set[str],
        expires: int | None,
        disclosure_access: dict | None = None,
    ) -> tuple[Generation, WakeupLease] | None:
        with database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = _row(db, owner, task_id)
            now = _now()
            if row is None or row["status"] not in {"pending", "dispatched"}:
                return None
            if row["lease_until"] is not None and row["lease_until"] > now:
                return None
            if (
                row["next_attempt_at"] is not None
                and row["next_attempt_at"] > now
            ):
                return None
            if row["status"] == "pending":
                if row["child_status"] == "running":
                    return None
                if row["child_status"] == "cancelled" or row["result_claimed"]:
                    _retire(db, task_id, "Child cancelled or already claimed")
                    return None
                setting = db.execute(
                    "SELECT value FROM settings WHERE owner=? AND key=?",
                    (owner, _COMPLETION_SETTING),
                ).fetchone()
                if (
                    setting is not None
                    and json.loads(setting["value"]) is False
                ):
                    _retire(db, task_id, "Completion wakeups disabled")
                    return None
                if (
                    row["grant_expires"] is not None
                    and row["grant_expires"] <= time.time()
                ):
                    _retire(db, task_id, "Original grant expired")
                    return None
                if not _child_is_owned(db, row):
                    _retire(
                        db, task_id, "Owned terminal child no longer available"
                    )
                    return None
                parent, parent_state, anchor = _source(
                    db,
                    row,
                    scopes=scopes,
                    expires=expires,
                    disclosure_access=disclosure_access,
                )
                if parent["status"] in {"running", "requires_action"}:
                    return None
                if parent["status"] != "complete":
                    _retire(db, task_id, "Original parent did not complete")
                    return None
                branch = _latest_branch(
                    db, row["parent_conversation_id"], anchor
                )
                if branch is None:
                    _retire(db, task_id, "Original parent reply unavailable")
                    return None
                # The native store is the only generation admission path.
                try:
                    generation, created = GenerationStore(
                        database
                    )._begin_locked(
                        db,
                        owner,
                        {
                            "conversationId": row["parent_conversation_id"],
                            "parentMessageId": branch,
                            "text": _prompt(row),
                            "agent_id": parent["agent_id"],
                            "provider": parent_state["provider"],
                            "model": parent_state["model"],
                            "lsmSampling": parent_state.get("sampling", {}),
                            "clientRequestId": f"subagent-wakeup:{task_id}:{row['requeue_count']}",
                        },
                        wakeup_task_id=task_id,
                    )
                except GenerationConflict:
                    return None
                if not created:
                    raise PermissionError(
                        "Completion request identity already exists"
                    )
                db.execute(
                    "UPDATE subagent_tasks SET result_claimed=1,result_claim_key=? WHERE id=?",
                    ("wakeup:" + task_id, task_id),
                )
            else:
                if not _child_is_owned(db, row):
                    raise PermissionError(
                        "Completion child no longer available"
                    )
                _source(
                    db,
                    row,
                    scopes=scopes,
                    expires=expires,
                    disclosure_access=disclosure_access,
                )
                run = db.execute(
                    "SELECT r.* FROM runs r JOIN conversations c ON c.id=r.conversation_id "
                    "WHERE r.id=? AND c.owner=? AND c.id=? AND json_extract(r.state,'$.completionWakeupTaskId')=?",
                    (
                        row["run_id"],
                        owner,
                        row["parent_conversation_id"],
                        task_id,
                    ),
                ).fetchone()
                if run is None or run["status"] != "running":
                    return None
                generation = GenerationStore._run(run)
            token = secrets.token_hex(24)
            db.execute(
                "UPDATE subagent_wakeups SET status='dispatched',run_id=?,lease_owner=?,lease_until=?,next_attempt_at=NULL,updated_at=? WHERE task_id=?",
                (generation.run_id, token, now + _LEASE_MS, now, task_id),
            )
            lease = cls(database, owner, task_id, generation.run_id, token)
            lease.actor.assert_current(db)
            return generation, lease

    def assert_current(
        self,
        *,
        scopes: set[str],
        expires: int | None,
        disclosure_access: dict | None = None,
        db=None,
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
        self.actor.assert_current(db)
        row = _row(db, self.owner, self.task_id)
        if not _child_is_owned(db, row):
            raise PermissionError("Completion child no longer available")
        _source(
            db,
            row,
            scopes=scopes,
            expires=expires,
            disclosure_access=disclosure_access,
        )

    def renew(self, **grant) -> None:
        with self.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self.assert_current(db=db, **grant)
            db.execute(
                "UPDATE subagent_wakeups SET lease_until=?,updated_at=? WHERE task_id=?",
                (_now() + _LEASE_MS, _now(), self.task_id),
            )

    def release(self) -> None:
        with self.database.connect() as db:
            db.execute(
                "UPDATE subagent_wakeups SET lease_owner=NULL,lease_until=NULL WHERE task_id=? AND owner=? AND run_id=? AND lease_owner=?",
                (self.task_id, self.owner, self.run_id, self.token),
            )
