"""Detached reservation lease and settlement; not an execution grant.

An authenticated host must supply its live grant at claim, renewal and every
execution boundary. A persisted task grant cannot authorize a new worker.
"""

import json
import secrets
import sqlite3
import time
import uuid
from dataclasses import dataclass

from .checkpoint_types import LoopSnapshot
from .database import SQLiteDatabase
from .detached_subagents import _MAX_THREAD_HISTORY
from .generation_actor_lease import ActorLease, ActorLeaseLost
from .generations import Generation, GenerationStore
from .provider_profiles import ProviderRepository
from .providers import selected_provider
from .storage import _now as _generation_now
from .subagent_scope import (
    DetachedExecutionScope,
    SubagentGraphScope,
    SubagentScope,
    validate_detached_descendants,
)
from .subagents import SubagentConfig, filter_subagent_result

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


def _child_run(db, task):
    row = db.execute(
        "SELECT r.* FROM runs r JOIN conversations c ON c.id=r.conversation_id "
        "WHERE r.id=? AND c.id=? AND c.owner=?",
        (task["child_run_id"], task["child_conversation_id"], task["owner"]),
    ).fetchone()
    if (
        row is None
        or json.loads(row["state"]).get("detachedTaskId") != task["id"]
    ):
        raise PermissionError("Detached child generation identity changed")
    return row


def _materialize(db, task):
    """Create only the reserved child; task binding and conversation are atomic."""
    if task["child_run_id"] is not None:
        return _child_run(db, task)
    agent = json.loads(task["agent_config"])
    options = json.loads(task["execution_options"])
    parent = db.execute(
        "SELECT state FROM runs WHERE id=?", (task["parent_run_id"],)
    ).fetchone()
    parent_state = json.loads(parent["state"])
    if options.get("ordinary_chat"):
        provider, model = parent_state["provider"], parent_state["model"]
        sampling = parent_state.get("sampling", {})
    else:
        provider = selected_provider({}, agent)
        profile = ProviderRepository._profile_record(db, provider)
        model = agent.get("model") or next(iter(profile["models"]), None)
        sampling = {}
    if not isinstance(model, str) or not model.strip():
        raise ValueError("Detached child requires a configured model")
    run_id = "run_" + secrets.token_hex(16)
    user_id, response_id = str(uuid.uuid4()), str(uuid.uuid4())
    conversation = task["child_conversation_id"]
    now = _generation_now()
    state = {
        "detachedTaskId": task["id"],
        "userMessageId": user_id,
        "responseMessageId": response_id,
        "generationCreatedAt": now // 1000,
        "generationProtocolVersion": 2,
        "provider": provider,
        "model": model,
        "sampling": sampling,
    }
    db.execute(
        "INSERT INTO conversations(id,owner,title,agent_id,model,endpoint,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
        (
            conversation,
            task["owner"],
            task["input"][:80],
            agent.get("id"),
            model,
            provider,
            now,
            now,
        ),
    )
    db.execute(
        "INSERT INTO messages(id,conversation_id,role,text,created_at,updated_at) VALUES(?,?,'user',?,?,?)",
        (user_id, conversation, task["input"], now, now),
    )
    db.execute(
        "INSERT INTO runs(id,conversation_id,agent_id,status,state,created_at,updated_at) VALUES(?,?,?,'running',?,?,?)",
        (
            run_id,
            conversation,
            agent.get("id"),
            json.dumps(state, allow_nan=False),
            now,
            now,
        ),
    )
    db.execute(
        "UPDATE subagent_tasks SET child_run_id=? WHERE id=?",
        (run_id, task["id"]),
    )
    return db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()


def _settle(
    database: SQLiteDatabase,
    db,
    row,
    status: str,
    *,
    result: str | None = None,
    history: str | None = None,
    error: str | None = None,
):
    if row["child_run_id"] is not None:
        child = _child_run(db, row)
        if child["status"] != "running":
            raise ActorLeaseLost("Detached child generation is already settled")
        GenerationStore(database)._finish_locked(
            db,
            child,
            status={
                "completed": "complete",
                "cancelled": "aborted",
                "error": "error",
            }[status],
            text=result or "",
            error=error,
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
        "UPDATE subagent_task_controls SET status='failed',reason='task_settled',updated_at=? WHERE task_id=? AND status='accepted'",
        (now, row["id"]),
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

    def _control_target(self, db, member: SubagentGraphScope | None):
        task = self._current(db)
        if task["child_run_id"] is None:
            if member is not None:
                raise PermissionError(
                    "Team control requires a materialized child"
                )
            return task, None, None
        scope = DetachedExecutionScope.load(
            db, self.owner, task["child_run_id"]
        )
        if member is None:
            if scope.team_definition_hash is not None:
                raise PermissionError(
                    "Team control requires a member activation"
                )
            return task, scope.checkpoint_namespace, None
        if member.team != scope or member.member_id is None:
            raise PermissionError("Control recipient belongs to another team")
        actor = ActorLease(
            self.database, self.owner, scope.run_id, self.token, self.task_id
        )
        member.resolve(db, actor)
        row = db.execute(
            "SELECT state FROM runs WHERE id=?", (scope.run_id,)
        ).fetchone()
        return task, member.checkpoint_namespace, json.loads(row["state"])

    def has_interrupt(
        self, *, member: SubagentGraphScope | None = None, **grant
    ) -> bool:
        if member is not None:
            # Elect the recipient before interrupting its stream. Siblings
            # cannot observe the same pending interrupt as their own.
            return bool(
                self.claim_controls(
                    "preempt", member=member, interrupt_only=True, **grant
                )
            )
        with self.database.read_snapshot() as db:
            self.assert_current(db=db, **grant)
            self._control_target(db, None)
            return (
                db.execute(
                    "SELECT 1 FROM subagent_task_controls WHERE task_id=? AND status='accepted' AND action='interrupt' LIMIT 1",
                    (self.task_id,),
                ).fetchone()
                is not None
            )

    def claim_controls(
        self,
        boundary: str,
        *,
        member: SubagentGraphScope | None = None,
        interrupt_only: bool = False,
        **grant,
    ) -> list[dict]:
        """Reserve delivery before saving; a replacement lease can retry it."""
        allowed = {
            "preempt": {"interrupt"},
            "tool": {"steer", "interrupt"},
            "turn": {"queue", "steer", "interrupt"},
        }[boundary]
        with self.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self.assert_current(db=db, **grant)
            task, namespace, state = self._control_target(db, member)
            routes = (
                state.setdefault("detachedControlRecipients", {})
                if state is not None
                else {}
            )
            rows = db.execute(
                "SELECT * FROM subagent_task_controls WHERE task_id=? AND status='accepted' ORDER BY created_at,rowid",
                (self.task_id,),
            ).fetchall()
            messages = []
            for row in rows:
                if interrupt_only and row["action"] != "interrupt":
                    continue
                if (
                    state is not None
                    and row["boundary"] is not None
                    and routes.get(row["id"]) != namespace
                ):
                    continue
                if row["boundary"] is None and row["action"] not in allowed:
                    continue
                db.execute(
                    "UPDATE subagent_task_controls SET boundary=coalesce(boundary,?),updated_at=? WHERE id=?",
                    (boundary, _now(), row["id"]),
                )
                if state is not None:
                    routes[row["id"]] = namespace
                messages.append(
                    {
                        "id": row["id"],
                        "role": "user",
                        "content": row["message"],
                        "additional_kwargs": {
                            "source": "steer",
                            "control_id": row["id"],
                        },
                    }
                )
            if state is not None and messages:
                db.execute(
                    "UPDATE runs SET state=? WHERE id=?",
                    (
                        json.dumps(state, separators=(",", ":")),
                        task["child_run_id"],
                    ),
                )
            return messages

    def acknowledge_controls(
        self, *, member: SubagentGraphScope | None = None, **grant
    ) -> None:
        """Acknowledge only exact messages already in the elected durable head."""
        with self.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self.assert_current(db=db, **grant)
            task, namespace, state = self._control_target(db, member)
            if namespace is None:
                return
            head = db.execute(
                "SELECT checkpoint FROM agent_checkpoints WHERE owner=? AND thread_id=? AND checkpoint_ns=? AND expires_at>? AND checkpoint IS NOT NULL ORDER BY checkpoint_id DESC LIMIT 1",
                (
                    self.owner,
                    task["child_conversation_id"],
                    namespace,
                    time.time(),
                ),
            ).fetchone()
            if head is None:
                return
            snapshot = LoopSnapshot.model_validate(
                json.loads(head["checkpoint"])["channel_values"]
            )
            messages = {
                message.get("id"): message
                for message in snapshot.messages
                if isinstance(message.get("id"), str)
            }
            routes = (
                state.get("detachedControlRecipients", {})
                if state is not None
                else {}
            )
            for row in db.execute(
                "SELECT * FROM subagent_task_controls WHERE task_id=? AND status='accepted' AND boundary IS NOT NULL",
                (self.task_id,),
            ):
                if state is not None and routes.get(row["id"]) != namespace:
                    continue
                expected = {
                    "id": row["id"],
                    "role": "user",
                    "content": row["message"],
                    "additional_kwargs": {
                        "source": "steer",
                        "control_id": row["id"],
                    },
                }
                if messages.get(row["id"]) == expected:
                    db.execute(
                        "UPDATE subagent_task_controls SET status='applied',updated_at=? WHERE id=?",
                        (_now(), row["id"]),
                    )
                    routes.pop(row["id"], None)
            if state is not None:
                db.execute(
                    "UPDATE runs SET state=? WHERE id=?",
                    (
                        json.dumps(state, separators=(",", ":")),
                        task["child_run_id"],
                    ),
                )

    def execution_input(
        self,
        *,
        scopes: set[str],
        expires: int | None,
        disclosure_access: dict | None = None,
    ) -> tuple[dict | None, list[dict], int | None]:
        with self.database.read_snapshot() as db:
            self.assert_current(
                scopes=scopes,
                expires=expires,
                disclosure_access=disclosure_access,
                db=db,
            )
            task = self._current(db)
            options = json.loads(task["execution_options"])
            return (
                None
                if options.get("ordinary_chat")
                else json.loads(task["agent_config"]),
                json.loads(task["initial_messages"]),
                options.get("recursion_limit"),
            )

    def child_catalogue(self, **grant) -> list[SubagentConfig]:
        with self.database.read_snapshot() as db:
            self.assert_current(db=db, **grant)
            options = json.loads(self._current(db)["execution_options"])
            return [
                SubagentConfig.from_record(value)
                for value in options["child_configs"]
            ]

    def materialize(
        self,
        *,
        scopes: set[str],
        expires: int | None,
        disclosure_access: dict | None = None,
    ) -> tuple[Generation, ActorLease]:
        """Bind the task's current token to its one private generation actor."""
        with self.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self.assert_current(
                scopes=scopes,
                expires=expires,
                disclosure_access=disclosure_access,
                db=db,
            )
            task = self._current(db)
            generation = GenerationStore._run(_materialize(db, task))
            actor = ActorLease(
                self.database,
                self.owner,
                generation.run_id,
                self.token,
                self.task_id,
            )
            actor.assert_current(db)
            return generation, actor

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
            options = json.loads(row["execution_options"])
            if options["child_configs"] and validate_detached_descendants(
                db,
                self.owner,
                self.task_id,
                row["child_run_id"],
                scopes,
                expires,
            ):
                options["nested_provenance"] = True
                db.execute(
                    "UPDATE subagent_tasks SET execution_options=? WHERE id=?",
                    (json.dumps(options, allow_nan=False), self.task_id),
                )
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
            _settle(
                self.database,
                db,
                row,
                "completed",
                result=result,
                history=encoded,
            )

    def fail(self) -> None:
        """Settle a failed worker without persisting provider error details."""
        with self.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._current(db)
            if row["cancel_requested"]:
                _settle(self.database, db, row, "cancelled")
            else:
                _settle(
                    self.database,
                    db,
                    row,
                    "error",
                    error="Detached subagent task failed",
                )

    def acknowledge_cancel(self) -> None:
        with self.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._current(db)
            if not row["cancel_requested"]:
                raise PermissionError(
                    "Detached task cancellation was not requested"
                )
            _settle(self.database, db, row, "cancelled")


def _request_cancel_locked(database: SQLiteDatabase, db, row) -> None:
    if row["status"] != "running":
        return
    db.execute(
        "UPDATE subagent_tasks SET cancel_requested=1,updated_at=? WHERE id=?",
        (_now(), row["id"]),
    )
    if row["lease_until"] is None or row["lease_until"] <= _now():
        _settle(database, db, row, "cancelled")


def request_cancel(
    database: SQLiteDatabase, owner: str, conversation: str, task_id: str
) -> None:
    """An owner can revoke a reservation even after its grant has expired."""
    with database.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = _task(db, owner, conversation, task_id)
        _request_cancel_locked(database, db, row)


def reconcile_reservations(
    database: SQLiteDatabase, owner: str, conversation: str
) -> int:
    """Retire idle cancelled/expired reservations, never launch a worker."""
    with database.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        rows = db.execute(
            "SELECT t.* FROM subagent_tasks t JOIN conversations c ON c.id=t.parent_conversation_id "
            "WHERE t.owner=? AND t.parent_conversation_id=? AND c.owner=t.owner AND t.status='running' "
            "AND (t.lease_until IS NULL OR t.lease_until<=?) "
            "AND (t.cancel_requested=1 OR t.grant_expires<=?) ORDER BY t.rowid LIMIT 100",
            (owner, conversation, _now(), time.time()),
        ).fetchall()
        for row in rows:
            if row["cancel_requested"]:
                _settle(database, db, row, "cancelled")
            else:
                _settle(
                    database,
                    db,
                    row,
                    "error",
                    error="Authentication grant expired",
                )
        return len(rows)
