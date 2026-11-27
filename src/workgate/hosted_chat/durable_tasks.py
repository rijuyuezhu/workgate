"""Durable native creation intents adapted from final upstream v6.0.3.

The intent is committed before native execution. Native IDs are observed from
structured results or a read-only lookup; never guessed from names or listings.
Source: webui/durable_tasks.py (9b50208b63aac266387b4e3b6b8e9ce6128b17e4).
"""

import hashlib
import json
from collections.abc import Collection
from dataclasses import dataclass, replace
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from ..protocol.executor import (
    ReservedJobStartRequest,
    ReservedShellStartRequest,
)
from ..protocol.ids import (
    ReservedJobId,
    ReservedShellId,
    SessionId,
    TaskId,
    new_command_id,
    new_reserved_job_id,
    new_reserved_shell_id,
    new_session_id,
    new_task_id,
)
from .database import SQLiteDatabase
from .generation_actor_lease import ActorLease, ActorLeaseLost
from .graph_member_scope import GraphMemberScope
from .graph_topology import graph_members
from .machine_permissions import authorize_machine_action, require_machine_scope
from .native_tool_kinds import (
    SESSION_COMMAND_EFFECT_TOOLS,
    SESSION_FILE_EFFECT_TOOLS,
    SESSION_METADATA_EFFECT_TOOLS,
    SESSION_SHELL_EFFECT_TOOLS,
    TASK_METADATA_EFFECT_TOOLS,
)
from .provider_common import ModelToolCall, complete_tool_batch
from .storage import WorkspaceRepository, _now
from .subagent_scope import SubagentGraphScope, SubagentScope


def _task_metadata_effect(call: ModelToolCall) -> bool:
    return call.name in TASK_METADATA_EFFECT_TOOLS and (
        call.name != "task"
        or call.arguments.get("action")
        in {"report", "block", "resume", "finish", "cancel", "delete"}
    )


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


_RETRY_REFUSED = "Native retry admission refused"


def _retry_targets(call: ModelToolCall) -> list[str]:
    arguments = call.arguments
    requested = arguments.get("retry")
    if (
        arguments.keys() - {"session_id", "retry"}
        or not isinstance(requested, list)
        or not requested
        or len(requested) > 64
    ):
        raise ValueError("Durable Job retry requires 1 to 64 Jobs")
    validator = TypeAdapter(ReservedJobId)
    targets = [
        validator.validate_python(item, strict=True) for item in requested
    ]
    if len(set(targets)) != len(targets):
        raise ValueError("Duplicate durable Job retry target")
    return targets


def reserved_job_request(
    call: ModelToolCall, key: str
) -> ReservedJobStartRequest:
    """Validate the same private wire request before claiming a native effect."""
    arguments = dict(call.arguments)
    if arguments.keys() & {"job_id", "tool", "idempotency_key"}:
        raise ValueError("Job identity is allocated by Control")
    TypeAdapter(SessionId).validate_python(
        arguments.pop("session_id", None), strict=True
    )
    if arguments.pop("pty", False) is not False:
        raise ValueError("Reserved Jobs cannot use PTY mode")
    purpose = arguments.pop("purpose", None)
    if purpose is not None and not isinstance(purpose, str):
        raise ValueError("Invalid tool purpose")
    return ReservedJobStartRequest.model_validate(
        {
            "job_id": key,
            "tool": call.name,
            **arguments,
        }
    )


def reserved_shell_request(
    call: ModelToolCall, key: str
) -> ReservedShellStartRequest:
    """Validate a PTY proposal against the native private start contract."""
    arguments = dict(call.arguments)
    if arguments.keys() & {"shell_id", "job_id", "tool", "idempotency_key"}:
        raise ValueError("Shell identity is allocated by Control")
    TypeAdapter(SessionId).validate_python(
        arguments.pop("session_id", None), strict=True
    )
    if (
        arguments.pop("pty", False) is not True
        or arguments.pop("async_", False) is not False
        or arguments.pop("persist_on_timeout", False) is not False
    ):
        raise ValueError("Reserved Shell requires PTY mode only")
    for field in ("name", "purpose"):
        value = arguments.pop(field, None)
        if value is not None and not isinstance(value, str):
            raise ValueError(f"Invalid Shell {field}")
    return ReservedShellStartRequest.model_validate(
        {"shell_id": key, "tool": call.name, **arguments}
    )


class ReservedCopyRequest(BaseModel):
    """Exact background copy arguments; native Job identity is private."""

    model_config = ConfigDict(extra="forbid", strict=True)
    src_session_id: SessionId
    src_path: str = Field(min_length=1)
    dst_session_id: SessionId
    dst_path: str = Field(min_length=1)
    kind: Literal["auto", "file", "dir"] = "auto"
    overwrite: bool = False
    chunk_size: int | None = Field(default=None, ge=1)
    background: bool = True

    @model_validator(mode="after")
    def require_background(self) -> Self:
        if not self.background:
            raise ValueError("Reserved copy requires background mode")
        return self


def _requirements(
    machine: str, kind: str, destination_machine: str | None
) -> dict[str, str]:
    if kind == "session_write":
        return {machine: "write"}
    if kind == "cancel":
        return (
            {machine: "execute"}
            if destination_machine is None
            else {machine: "read", destination_machine: "write"}
        )
    if kind == "poll":
        return (
            {machine: "read"}
            if destination_machine is None
            else {machine: "read", destination_machine: "read"}
        )
    if kind == "copy" or (kind == "retry" and destination_machine is not None):
        if destination_machine is None:
            raise PermissionError("Missing destination Executor binding")
        return {machine: "read", destination_machine: "write"}
    return {machine: "execute" if kind in {"job", "shell", "retry"} else "read"}


@dataclass(frozen=True)
class Intent:
    id: str
    name: str
    key: str
    machine: str
    state: str
    native_id: str | None
    session_id: str | None
    target_job_id: str | None
    attempt: int | None
    destination_session_id: str | None
    destination_machine: str | None


class DurableTaskRepository:
    """Private intent ledger; Control/Executor remain the native resource owners.

    A claimed creation is uncertain until the native identity is confirmed.
    Even a missing receipt does not authorize a second mutation.
    """

    def __init__(
        self,
        database: SQLiteDatabase,
        *,
        scope: GraphMemberScope
        | SubagentScope
        | SubagentGraphScope
        | None = None,
    ):
        self.database = database
        self.scope = scope

    def cap_grant(
        self, scopes: set[str], expires: int | None
    ) -> tuple[set[str], int | None]:
        return (
            self.scope.effective_grant(scopes, expires)
            if isinstance(self.scope, (SubagentScope, SubagentGraphScope))
            else (scopes, expires)
        )

    def _ledger_call(self, call: ModelToolCall) -> ModelToolCall:
        return (
            call
            if self.scope is None
            else replace(call, call_id=self.scope.native_call_id(call.call_id))
        )

    @staticmethod
    def _intent(row) -> Intent:
        return Intent(
            row["id"],
            row["tool"],
            row["idempotency_key"],
            row["machine"],
            row["state"],
            row["native_id"],
            row["native_session_id"],
            row["target_job_id"],
            row["native_attempt"],
            row["destination_session_id"],
            row["destination_machine"],
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
        if self.scope is not None:
            self.scope.resolve(db, lease)
        return {
            **dict(row),
            "member_namespace": self.scope.checkpoint_namespace
            if self.scope
            else None,
            "member_agent_id": self.scope.member_id
            if self.scope is not None
            else None,
        }

    @staticmethod
    def _agent_for_run(db, run) -> dict | None:
        agent = None
        if run["agent_id"] is not None:
            row = db.execute(
                "SELECT config FROM agents WHERE id=? AND owner=?",
                (run["agent_id"], run["owner"]),
            ).fetchone()
            if row is None:
                raise PermissionError("Generation Agent no longer exists")
            agent = json.loads(row[0])
        return agent

    @staticmethod
    def _child_scope_for_run(
        db, run
    ) -> SubagentScope | SubagentGraphScope | None:
        namespace = run["member_namespace"]
        if namespace is None or ":child:" not in namespace:
            return None
        fields = dict(run)
        run_id = fields.get("run_id", fields["id"])
        row = db.execute(
            "SELECT r.state FROM runs r JOIN conversations c ON c.id=r.conversation_id WHERE r.id=? AND c.owner=?",
            (run_id, run["owner"]),
        ).fetchone()
        if row is None:
            raise PermissionError("Native child generation no longer exists")
        try:
            child_namespace, separator, activation = namespace.partition(
                ":graph:team:member:"
            )
            record = json.loads(row["state"])["ownedSubagentScopes"][
                child_namespace
            ]
            child = SubagentScope.from_record(record)
            scope = (
                SubagentGraphScope(child, run["member_agent_id"], activation)
                if separator
                else child
            )
            if (
                child.run_id != run_id
                or scope.checkpoint_namespace != namespace
                or scope.member_id != run["member_agent_id"]
            ):
                raise PermissionError("Native child scope identity changed")
            return scope
        except (KeyError, TypeError, ValueError) as error:
            raise PermissionError(
                "Native child scope admission is unavailable"
            ) from error

    @classmethod
    def _agents_for_run(cls, db, run) -> tuple[dict | None, ...]:
        scope = cls._child_scope_for_run(db, run)
        if scope is not None:
            return scope.resolve_owned(
                db,
                run["owner"],
                run["run_id"] if "run_id" in dict(run) else run["id"],
            )
        root = cls._agent_for_run(db, run)
        member_id = run["member_agent_id"]
        if member_id is None or member_id == run["agent_id"]:
            return (root,)
        if root is None or member_id not in graph_members(
            {**root, "id": run["agent_id"]}
        ):
            raise PermissionError(
                "Graph member is no longer in the original Agent graph"
            )
        row = db.execute(
            "SELECT config FROM agents WHERE id=? AND owner=?",
            (member_id, run["owner"]),
        ).fetchone()
        if row is None:
            raise PermissionError("Graph member Agent no longer exists")
        return root, json.loads(row["config"])

    def tool_context_chain(
        self, lease: ActorLease
    ) -> tuple[str, tuple[dict | None, ...]]:
        with self.database.read_snapshot() as db:
            run = self._run(db, lease)
            return run["conversation_id"], self._agents_for_run(db, run)

    def tool_context(self, lease: ActorLease) -> tuple[str, dict | None]:
        """Resolve the current generation and owner-scoped member selection."""
        conversation, chain = self.tool_context_chain(lease)
        return conversation, chain[-1]

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
        destination_machine: str | None = None,
    ) -> None:
        child_scope = self._child_scope_for_run(db, run)
        if child_scope is not None:
            scopes, expires = child_scope.effective_grant(scopes, expires)
        chain = self._agents_for_run(db, run)
        # Task is Control-owned metadata, but reserve it only for a selected
        # authorized Executor; no implicit Controller machine target.
        for target, operation in _requirements(
            machine, kind, destination_machine
        ).items():
            authorize_machine_action(
                run["owner"],
                authorization_id,
                target,
                operation,
                agent=chain[-1],
                ancestors=chain[:-1],
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
        session_id: str | None = None,
    ) -> bool:
        if kind in {"job", "shell", "retry", "copy"} and session_id is None:
            return False
        if kind != "task" and machine is None:
            return False
        return (
            db.execute(
                "SELECT 1 FROM agent_durable_tasks WHERE owner=? AND conversation_id=? "
                "AND (kind=? OR (?='job' AND kind IN ('retry','copy'))) AND native_id=? AND state='active' "
                "AND (? IS NULL OR machine=?) AND (? IS NULL OR native_session_id=?) LIMIT 1",
                (
                    owner,
                    conversation_id,
                    kind,
                    kind,
                    native_id,
                    machine,
                    machine,
                    session_id,
                    session_id,
                ),
            ).fetchone()
            is not None
        )

    def _require_parent(
        self,
        db,
        run,
        kind: str,
        machine: str,
        arguments: dict,
        destination_machine: str | None = None,
        destination_session_id: str | None = None,
    ) -> None:
        if kind == "session" and not self._owned(
            db,
            run["owner"],
            run["conversation_id"],
            "task",
            arguments["task_id"],
        ):
            raise PermissionError(
                "Native Task does not belong to this conversation"
            )
        if kind in {"job", "shell", "retry", "copy"}:
            source = (
                arguments["src_session_id"]
                if kind == "copy"
                else arguments["session_id"]
            )
            if not self._owned(
                db,
                run["owner"],
                run["conversation_id"],
                "session",
                source,
                machine,
            ):
                raise PermissionError(
                    "Native Session does not belong to this conversation and Executor"
                )
        if kind == "copy" or (
            kind == "retry" and destination_machine is not None
        ):
            if destination_session_id is None or destination_machine is None:
                raise PermissionError("Missing destination Session binding")
            if not self._owned(
                db,
                run["owner"],
                run["conversation_id"],
                "session",
                destination_session_id,
                destination_machine,
            ):
                raise PermissionError(
                    "Managed copy Session does not belong to this conversation and Executor"
                )

    @staticmethod
    def _last_owned_job(db, run, machine: str, session_id: str, job_id: str):
        row = db.execute(
            "SELECT * FROM agent_durable_tasks "
            "WHERE owner=? AND conversation_id=? AND machine=? AND native_session_id=? "
            "AND native_id=? AND kind IN ('job','retry','copy') AND state IN ('active','stopped') "
            "ORDER BY native_attempt DESC LIMIT 1",
            (run["owner"], run["conversation_id"], machine, session_id, job_id),
        ).fetchone()
        if row is None or row["native_attempt"] is None:
            raise PermissionError(
                "Native Job does not belong to this conversation"
            )
        return row

    def resolve_session_executor(
        self,
        lease: ActorLease,
        session_id: object,
        *,
        closing: bool = False,
    ) -> str:
        """Resolve a confirmed owned Session without consulting native inventory."""
        session_id = TypeAdapter(SessionId).validate_python(
            session_id, strict=True
        )
        states = (
            ("active", "cancel_pending", "stopped") if closing else ("active",)
        )
        with self.database.read_snapshot() as db:
            run = self._run(db, lease)
            rows = db.execute(
                "SELECT DISTINCT machine FROM agent_durable_tasks WHERE owner=? AND conversation_id=? "
                "AND kind='session' AND native_id=? AND idempotency_key=native_id "
                f"AND state IN ({','.join('?' for _ in states)})",
                (lease.owner, run["conversation_id"], session_id, *states),
            ).fetchall()
            if len(rows) != 1:
                raise PermissionError(
                    "Native Session does not belong to this conversation"
                )
            return rows[0]["machine"]

    def authorize_session_read(
        self,
        lease: ActorLease,
        session_id: str,
        *,
        machine: str,
        scopes: set[str],
        expires: int | None,
        registered: Collection[str],
        inspect_shells: bool = False,
    ) -> frozenset[str]:
        """Require a live generation and its confirmed native Session."""
        with self.database.read_snapshot() as db:
            run = self._run(db, lease)
            scopes, expires = self.cap_grant(scopes, expires)
            if not self._owned(
                db,
                lease.owner,
                run["conversation_id"],
                "session",
                session_id,
                machine,
            ):
                raise PermissionError(
                    "Native Session does not belong to this conversation and Executor"
                )
            self._authorize(
                db,
                run,
                machine,
                "session_read",
                scopes,
                expires,
                registered,
                run["conversation_id"],
            )
            if not inspect_shells:
                return frozenset()
            return frozenset(
                row["native_id"]
                for row in db.execute(
                    "SELECT native_id FROM agent_durable_tasks WHERE owner=? AND conversation_id=? "
                    "AND machine=? AND native_session_id=? AND kind='shell' AND state='active' AND native_id IS NOT NULL",
                    (lease.owner, run["conversation_id"], machine, session_id),
                )
            )

    def poll_attempts(
        self,
        lease: ActorLease,
        session_id: str,
        targets: list[str],
        *,
        machine: str,
        scopes: set[str],
        expires: int | None,
        registered: Collection[str],
    ) -> list[Intent]:
        """Resolve exact owned attempts and read grants before any native lookup."""
        with self.database.read_snapshot() as db:
            run = self._run(db, lease)
            scopes, expires = self.cap_grant(scopes, expires)
            result = []
            for target in targets:
                row = self._last_owned_job(db, run, machine, session_id, target)
                self._require_parent(
                    db,
                    run,
                    "retry",
                    machine,
                    {"session_id": session_id},
                    row["destination_machine"],
                    row["destination_session_id"],
                )
                self._authorize(
                    db,
                    run,
                    machine,
                    "poll",
                    scopes,
                    expires,
                    registered,
                    run["conversation_id"],
                    row["destination_machine"],
                )
                result.append(self._intent(row))
            return result

    def list_attempts(
        self,
        lease: ActorLease,
        session_id: str,
        *,
        machine: str,
        scopes: set[str],
        expires: int | None,
        registered: Collection[str],
    ) -> list[Intent]:
        """List confirmed owned attempts, excluding unauthorized destinations."""
        with self.database.read_snapshot() as db:
            run = self._run(db, lease)
            scopes, expires = self.cap_grant(scopes, expires)
            self._require_parent(
                db, run, "job", machine, {"session_id": session_id}
            )
            self._authorize(
                db,
                run,
                machine,
                "poll",
                scopes,
                expires,
                registered,
                run["conversation_id"],
            )
            rows = db.execute(
                "SELECT * FROM (SELECT *, ROW_NUMBER() OVER "
                "(PARTITION BY native_id ORDER BY native_attempt DESC, id DESC) AS rank "
                "FROM agent_durable_tasks WHERE owner=? AND conversation_id=? "
                "AND machine=? AND native_session_id=? AND native_id IS NOT NULL "
                "AND native_attempt IS NOT NULL AND kind IN ('job','retry','copy') "
                "AND state IN ('active','stopped')) WHERE rank=1 ORDER BY native_id",
                (run["owner"], run["conversation_id"], machine, session_id),
            )
            result = []
            for row in rows:
                try:
                    self._require_parent(
                        db,
                        run,
                        "retry",
                        machine,
                        {"session_id": session_id},
                        row["destination_machine"],
                        row["destination_session_id"],
                    )
                    self._authorize(
                        db,
                        run,
                        machine,
                        "poll",
                        scopes,
                        expires,
                        registered,
                        run["conversation_id"],
                        row["destination_machine"],
                    )
                except PermissionError:
                    continue
                result.append(self._intent(row))
            return result

    def _authorize_task_metadata(
        self,
        db,
        run,
        task_id: str,
        scopes: set[str],
        expires: int | None,
        *,
        write: bool,
    ) -> frozenset[str]:
        if not self._owned(
            db, run["owner"], run["conversation_id"], "task", task_id
        ):
            raise PermissionError(
                "Native Task does not belong to this conversation"
            )
        require_machine_scope(
            scopes, "write" if write else "read", expires=expires
        )
        return frozenset(
            row[0]
            for row in db.execute(
                "SELECT native_id FROM agent_durable_tasks WHERE owner=? AND conversation_id=? "
                "AND kind='session' AND state='active' AND native_id IS NOT NULL",
                (run["owner"], run["conversation_id"]),
            )
        )

    def authorize_task_metadata(
        self,
        lease: ActorLease,
        task_id: str,
        *,
        scopes: set[str],
        expires: int | None,
        write: bool,
    ) -> frozenset[str]:
        with self.database.read_snapshot() as db:
            run = self._run(db, lease)
            scopes, expires = self.cap_grant(scopes, expires)
            return self._authorize_task_metadata(
                db, run, task_id, scopes, expires, write=write
            )

    def native_effect(
        self,
        lease: ActorLease,
        call: ModelToolCall,
        *,
        machine: str,
        scopes: set[str],
        expires: int | None,
        registered: Collection[str],
        claim: bool = False,
        receipt: dict | None = None,
    ) -> tuple[bool, dict | None]:
        """Claim a finite native effect once, or check/settle that exact claim.

        A NULL receipt is permanently uncertain: recovery must not repeat a mutation.
        Unlike durable resource starts, these effects have no lookup receipt.
        """
        complete_tool_batch([call])
        call = self._ledger_call(call)
        task_metadata = _task_metadata_effect(call)
        deleting_task = (
            task_metadata
            and call.name == "task"
            and call.arguments.get("action") == "delete"
        )
        session_metadata = call.name in SESSION_METADATA_EFFECT_TOOLS
        shell = call.name in SESSION_SHELL_EFFECT_TOOLS
        command = call.name in SESSION_COMMAND_EFFECT_TOOLS
        if command and any(
            call.arguments.get(flag, False) is not False
            for flag in ("async_", "pty", "persist_on_timeout")
        ):
            raise ValueError(
                "Tracked or PTY execution requires native reservation"
            )
        if (
            not (shell or command or task_metadata or session_metadata)
            and call.name not in SESSION_FILE_EFFECT_TOOLS
        ):
            raise ValueError("Expected a finite native Session effect")
        task_id = (
            TypeAdapter(TaskId).validate_python(
                call.arguments.get("task_id"), strict=True
            )
            if task_metadata
            else None
        )
        if task_metadata and machine != "control":
            raise ValueError("Task metadata is Control-owned")
        session_id = (
            None
            if task_metadata
            else TypeAdapter(SessionId).validate_python(
                call.arguments.get("session_id"), strict=True
            )
        )
        shell_id = (
            TypeAdapter(ReservedShellId).validate_python(
                call.arguments.get("shell_id"), strict=True
            )
            if shell
            else None
        )
        fingerprint = _fingerprint(call.name, call.arguments)
        with (
            self.database.connect()
            if claim or receipt is not None
            else self.database.read_snapshot()
        ) as db:
            if claim or receipt is not None:
                db.execute("BEGIN IMMEDIATE")
            run = self._run(db, lease)
            scopes, expires = self.cap_grant(scopes, expires)
            prior = db.execute(
                "SELECT * FROM agent_native_effect_calls WHERE run_id=? AND call_id=?",
                (lease.run_id, call.call_id),
            ).fetchone()
            if prior is not None:
                if (
                    prior["fingerprint"] != fingerprint
                    or prior["machine"] != machine
                ):
                    raise PermissionError(
                        "A replayed tool call cannot change a durable request"
                    )
                scopes, expires = self._grant(prior, scopes, expires)
            elif not claim:
                raise PermissionError("Native Session effect was not claimed")
            if db.execute(
                "SELECT 1 FROM agent_durable_tasks WHERE owner=? AND run_id=? AND call_id=? "
                "UNION ALL SELECT 1 FROM agent_job_cancel_calls WHERE run_id=? AND call_id=?",
                (
                    lease.owner,
                    lease.run_id,
                    call.call_id,
                    lease.run_id,
                    call.call_id,
                ),
            ).fetchone():
                raise PermissionError(
                    "A replayed tool call cannot change a durable request"
                )
            if task_id is not None:
                if (
                    deleting_task
                    and prior is not None
                    and prior["receipt"] is not None
                ):
                    # The confirmed deletion retired the Task ownership row.
                    # Only this exact immutable call may disclose its receipt.
                    require_machine_scope(scopes, "write", expires=expires)
                else:
                    self._authorize_task_metadata(
                        db, run, task_id, scopes, expires, write=True
                    )
            else:
                self._require_parent(
                    db, run, "shell", machine, {"session_id": session_id}
                )
                if shell_id is not None and not self._owned(
                    db,
                    lease.owner,
                    run["conversation_id"],
                    "shell",
                    shell_id,
                    machine,
                    session_id,
                ):
                    raise PermissionError(
                        "Native Shell does not belong to this conversation"
                    )
                self._authorize(
                    db,
                    run,
                    machine,
                    "shell"
                    if shell or command
                    else (
                        "session_read" if session_metadata else "session_write"
                    ),
                    scopes,
                    expires,
                    registered,
                    run["conversation_id"],
                )
            if prior is None:
                db.execute(
                    "INSERT INTO agent_native_effect_calls(run_id,call_id,fingerprint,machine,scopes,grant_expires) "
                    "VALUES(?,?,?,?,?,?)",
                    (
                        lease.run_id,
                        call.call_id,
                        fingerprint,
                        machine,
                        json.dumps(sorted(scopes)),
                        expires,
                    ),
                )
                return True, None
            if receipt is not None:
                if claim or prior["receipt"] is not None:
                    raise ValueError("Native effect is already settled")
                db.execute(
                    "UPDATE agent_native_effect_calls SET receipt=? WHERE run_id=? AND call_id=?",
                    (
                        json.dumps(receipt, allow_nan=False),
                        lease.run_id,
                        call.call_id,
                    ),
                )
                if deleting_task:
                    db.execute(
                        "UPDATE agent_durable_tasks SET state='stopped',updated_at=?,next_attempt_at=NULL "
                        "WHERE owner=? AND conversation_id=? AND kind='task' AND native_id=? AND state='active'",
                        (_now(), lease.owner, run["conversation_id"], task_id),
                    )
                return False, receipt
            return False, json.loads(prior["receipt"]) if prior[
                "receipt"
            ] is not None else None

    def cancel_attempts(
        self,
        lease: ActorLease,
        call: ModelToolCall,
        *,
        machine: str,
        scopes: set[str],
        expires: int | None,
        registered: Collection[str],
    ) -> list[dict]:
        """Atomically authorize and latch a whole immutable cancellation batch."""
        complete_tool_batch([call])
        call = self._ledger_call(call)
        arguments = call.arguments
        shell = call.name == "kill_persistent_shell"
        session = call.name == "session_end"
        if session:
            if (
                arguments.keys() - {"session_id", "force"}
                or arguments.get("force", False) is not False
            ):
                raise ValueError(
                    "Durable Session close requires confirmed native absence; force is unsupported"
                )
            targets = [
                TypeAdapter(SessionId).validate_python(
                    arguments.get("session_id"), strict=True
                )
            ]
        elif shell:
            if arguments.keys() != {"session_id", "shell_id"}:
                raise ValueError(
                    "Durable Shell cancellation requires one exact Shell"
                )
            targets = [
                TypeAdapter(ReservedShellId).validate_python(
                    arguments["shell_id"],
                    strict=True,
                )
            ]
        else:
            targets = arguments.get("cancel")
            if (
                call.name != "job"
                or arguments.keys() - {"session_id", "cancel"}
                or not isinstance(targets, list)
                or not 1 <= len(targets) <= 64
            ):
                raise ValueError("Durable Job cancel requires 1 to 64 Jobs")
            targets = [
                TypeAdapter(ReservedJobId).validate_python(target, strict=True)
                for target in targets
            ]
            if len(set(targets)) != len(targets):
                raise ValueError("Duplicate durable Job cancel target")
        session_id = TypeAdapter(SessionId).validate_python(
            arguments.get("session_id"), strict=True
        )
        kinds = (
            ("session",)
            if session
            else (("shell",) if shell else ("job", "retry", "copy"))
        )
        parent_session = None if session else session_id
        fingerprint = _fingerprint(call.name, arguments)
        with self.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            run = self._run(db, lease)
            scopes, expires = self.cap_grant(scopes, expires)
            if db.execute(
                "SELECT 1 FROM agent_native_effect_calls WHERE run_id=? AND call_id=?",
                (lease.run_id, call.call_id),
            ).fetchone():
                raise PermissionError(
                    "A replayed tool call cannot change a durable request"
                )
            if db.execute(
                "SELECT 1 FROM agent_durable_tasks WHERE owner=? AND run_id=? AND call_id=?",
                (lease.owner, lease.run_id, call.call_id),
            ).fetchone():
                raise PermissionError(
                    "A replayed tool call cannot change a durable request"
                )
            prior = db.execute(
                "SELECT * FROM agent_job_cancel_calls WHERE run_id=? AND call_id=?",
                (lease.run_id, call.call_id),
            ).fetchone()
            if prior is not None:
                if prior["fingerprint"] != fingerprint:
                    raise PermissionError(
                        "A replayed tool call cannot change a durable request"
                    )
                scopes, expires = self._grant(prior, scopes, expires)
                identities = json.loads(prior["intent_ids"])
                rows = [
                    db.execute(
                        "SELECT * FROM agent_durable_tasks WHERE id=?",
                        (identity,),
                    ).fetchone()
                    for identity in identities
                ]
            else:
                rows = [
                    db.execute(
                        "SELECT * FROM agent_durable_tasks WHERE owner=? AND conversation_id=? "
                        "AND machine=? AND native_session_id IS ? AND native_id=? "
                        f"AND kind IN ({','.join('?' for _ in kinds)}) AND state IN ('active','stopped','cancel_pending') "
                        "ORDER BY native_attempt DESC LIMIT 1",
                        (
                            run["owner"],
                            run["conversation_id"],
                            machine,
                            parent_session,
                            target,
                            *kinds,
                        ),
                    ).fetchone()
                    for target in targets
                ]
            for target, row in zip(targets, rows, strict=True):
                if (
                    row is None
                    or row["owner"] != lease.owner
                    or row["conversation_id"] != run["conversation_id"]
                    or row["machine"] != machine
                    or row["native_session_id"] != parent_session
                    or row["native_id"] != target
                    or row["kind"] not in kinds
                    or (
                        not (shell or session) and row["native_attempt"] is None
                    )
                    or row["state"]
                    not in {"active", "stopped", "cancel_pending"}
                ):
                    raise PermissionError(
                        f"Native {'Session' if session else ('Shell' if shell else 'Job')} does not belong to this conversation"
                    )
                if not session:
                    self._require_parent(
                        db,
                        run,
                        "shell" if shell else "retry",
                        machine,
                        {"session_id": session_id},
                        row["destination_machine"],
                        row["destination_session_id"],
                    )
                self._authorize(
                    db,
                    run,
                    machine,
                    "cancel",
                    scopes,
                    expires,
                    registered,
                    run["conversation_id"],
                    row["destination_machine"],
                )
            if prior is None:
                db.execute(
                    "INSERT INTO agent_job_cancel_calls(run_id,call_id,fingerprint,intent_ids,scopes,grant_expires) VALUES(?,?,?,?,?,?)",
                    (
                        lease.run_id,
                        call.call_id,
                        fingerprint,
                        json.dumps([row["id"] for row in rows]),
                        json.dumps(sorted(scopes)),
                        expires,
                    ),
                )
            for row in rows:
                db.execute(
                    "UPDATE agent_durable_tasks SET state='cancel_pending',updated_at=?,next_attempt_at=NULL "
                    "WHERE id=? AND state='active'",
                    (_now(), row["id"]),
                )
                if session:
                    db.execute(
                        "UPDATE agent_durable_tasks SET state=CASE WHEN state='reserved' THEN 'stopped' ELSE 'cancel_pending' END,"
                        "updated_at=?,next_attempt_at=NULL WHERE owner=? AND conversation_id=? "
                        "AND kind IN ('job','shell','retry','copy') AND state IN ('reserved','active','unknown') "
                        "AND ((native_session_id=? AND machine=?) OR (destination_session_id=? AND destination_machine=?))",
                        (
                            _now(),
                            lease.owner,
                            run["conversation_id"],
                            session_id,
                            machine,
                            session_id,
                            machine,
                        ),
                    )
            return [dict(row) for row in rows]

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
        destination_machine: str | None = None,
    ) -> Intent:
        """Reserve one stable identity for the immutable native creation request."""
        complete_tool_batch([call])
        if call.name == "job" and len(_retry_targets(call)) != 1:
            raise ValueError("Use batch reservation for multiple Jobs")
        return self.reserve_all(
            lease,
            call,
            machine=machine,
            scopes=scopes,
            expires=expires,
            registered=registered,
            authorization_conversation_id=authorization_conversation_id,
            destination_machine=destination_machine,
        )[0]

    def reserve_all(
        self,
        lease: ActorLease,
        call: ModelToolCall,
        *,
        machine: str,
        scopes: set[str],
        expires: int | None,
        registered: Collection[str],
        authorization_conversation_id: str | None = None,
        destination_machine: str | None = None,
    ) -> list[Intent]:
        """Reserve the complete call atomically, one intent per retry target."""
        complete_tool_batch([call])
        call = self._ledger_call(call)
        targets = _retry_targets(call) if call.name == "job" else [None]
        fingerprint = _fingerprint(call.name, call.arguments)
        with self.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            run = self._run(db, lease)
            scopes, expires = self.cap_grant(scopes, expires)
            if db.execute(
                "SELECT 1 FROM agent_native_effect_calls WHERE run_id=? AND call_id=?",
                (lease.run_id, call.call_id),
            ).fetchone():
                raise PermissionError(
                    "A replayed tool call cannot change a durable request"
                )
            if db.execute(
                "SELECT 1 FROM agent_job_cancel_calls WHERE run_id=? AND call_id=?",
                (lease.run_id, call.call_id),
            ).fetchone():
                raise PermissionError(
                    "A replayed tool call cannot change a durable request"
                )
            prior_rows = db.execute(
                "SELECT fingerprint FROM agent_durable_tasks WHERE owner=? AND run_id=? AND call_id=?",
                (lease.owner, lease.run_id, call.call_id),
            ).fetchall()
            if prior_rows and (
                len(prior_rows) != len(targets)
                or any(row["fingerprint"] != fingerprint for row in prior_rows)
            ):
                raise PermissionError(
                    "A replayed tool call cannot change a durable request"
                )
            return [
                self._reserve_one(
                    db,
                    run,
                    lease,
                    call,
                    machine=machine,
                    scopes=scopes,
                    expires=expires,
                    registered=registered,
                    authorization_conversation_id=authorization_conversation_id,
                    destination_machine=destination_machine,
                    target_job_id=target,
                    batch=len(targets) > 1,
                    fingerprint=fingerprint,
                )
                for target in targets
            ]

    def _reserve_one(
        self,
        db,
        run,
        lease: ActorLease,
        call: ModelToolCall,
        *,
        machine: str,
        scopes: set[str],
        expires: int | None,
        registered: Collection[str],
        authorization_conversation_id: str | None,
        destination_machine: str | None,
        target_job_id: str | None,
        batch: bool,
        fingerprint: str,
    ) -> Intent:
        arguments = call.arguments
        session_id = None
        destination_session_id = None
        attempt = None
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
        elif call.name in {"bash", "run_python_code"}:
            kind = "shell" if arguments.get("pty") is True else "job"
            session_id = arguments.get("session_id")
            if kind == "job":
                attempt = 1
        elif call.name == "session_copy":
            kind = "copy"
            request = ReservedCopyRequest.model_validate(arguments)
            session_id = request.src_session_id
            destination_session_id = request.dst_session_id
            attempt = 1
            if destination_machine is None:
                raise PermissionError("Missing destination Executor binding")
        elif call.name == "job":
            kind = "retry"
            session_id = TypeAdapter(SessionId).validate_python(
                arguments.get("session_id"), strict=True
            )
            assert target_job_id is not None
        else:
            raise ValueError("Not a supported native creation")
        if kind != "copy" and destination_machine is not None:
            raise ValueError("Unexpected destination Executor binding")
        if kind == "shell":
            name = arguments.get("name")
            if name is not None and not isinstance(name, str):
                raise ValueError("Invalid Shell name")
            key = new_reserved_shell_id(name)
            reserved_shell_request(call, key)
        else:
            key = {
                "task": new_task_id,
                "session": new_session_id,
                "job": new_reserved_job_id,
                "retry": new_command_id,
                "copy": new_reserved_job_id,
            }[kind]()
            if kind == "job":
                reserved_job_request(call, key)
        identity = _fingerprint(
            "native-intent",
            {
                "owner": lease.owner,
                "run": lease.run_id,
                "call": call.call_id,
                **({"target": target_job_id} if batch else {}),
            },
        )
        conversation_id = run["conversation_id"]
        authorization_id = authorization_conversation_id or conversation_id
        if authorization_id != conversation_id:
            raise PermissionError(
                "Inherited authorization requires a verified child lineage"
            )
        prior = db.execute(
            "SELECT * FROM agent_durable_tasks WHERE id=?", (identity,)
        ).fetchone()
        last_attempt = None
        if kind == "retry":
            assert session_id is not None and target_job_id is not None
            owned = self._last_owned_job(
                db, run, machine, session_id, target_job_id
            )
            last_attempt = int(owned["native_attempt"])
            destination_session_id = owned["destination_session_id"]
            destination_machine = owned["destination_machine"]
        if prior is not None:
            if (
                prior["fingerprint"] != fingerprint
                or prior["machine"] != machine
                or prior["native_session_id"] != session_id
                or prior["destination_session_id"] != destination_session_id
                or prior["destination_machine"] != destination_machine
                or prior["target_job_id"] != target_job_id
                or prior["agent_id"] != run["agent_id"]
                or prior["member_agent_id"] != run["member_agent_id"]
                or prior["member_namespace"] != run["member_namespace"]
                or prior["authorization_conversation_id"] != authorization_id
            ):
                raise PermissionError(
                    "A replayed tool call cannot change a durable request"
                )
            rejected = (
                batch
                and prior["state"] == "stopped"
                and prior["native_id"] is None
                and prior["last_error"] == _RETRY_REFUSED
            )
            if (
                prior["state"] not in {"reserved", "unknown", "active"}
                and not rejected
            ):
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
            destination_machine,
        )
        self._require_parent(
            db,
            run,
            kind,
            machine,
            arguments,
            destination_machine,
            destination_session_id,
        )
        if kind == "retry":
            assert last_attempt is not None
            if prior is None:
                if db.execute(
                    "SELECT 1 FROM agent_durable_tasks WHERE machine=? AND native_session_id=? "
                    "AND target_job_id=? AND state IN ('reserved','unknown','cancel_pending') LIMIT 1",
                    (machine, session_id, target_job_id),
                ).fetchone():
                    raise PermissionError(
                        "Previous native Job retry is unresolved"
                    )
                attempt = last_attempt + 1
        if prior is not None:
            return self._intent(prior)
        now = _now()
        db.execute(
            "INSERT INTO agent_durable_tasks(id,owner,conversation_id,authorization_conversation_id,"
            "run_id,call_id,agent_id,member_agent_id,member_namespace,tool,kind,machine,native_session_id,destination_session_id,destination_machine,target_job_id,native_attempt,requirements,fingerprint,idempotency_key,key_hash,"
            "scopes,grant_expires,state,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                identity,
                lease.owner,
                conversation_id,
                authorization_id,
                lease.run_id,
                call.call_id,
                run["agent_id"],
                run["member_agent_id"],
                run["member_namespace"],
                call.name,
                kind,
                machine,
                session_id,
                destination_session_id,
                destination_machine,
                target_job_id,
                attempt,
                json.dumps(_requirements(machine, kind, destination_machine)),
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

    def _row(self, db, lease: ActorLease, intent: Intent):
        if self.scope is not None:
            self.scope.resolve(db, lease)
        row = db.execute(
            "SELECT * FROM agent_durable_tasks WHERE id=? AND owner=? AND run_id=?",
            (intent.id, lease.owner, lease.run_id),
        ).fetchone()
        if (
            row is None
            or row["member_agent_id"]
            != (self.scope.member_id if self.scope is not None else None)
            or row["member_namespace"]
            != (self.scope.checkpoint_namespace if self.scope else None)
            or row["idempotency_key"] != intent.key
            or row["tool"] != intent.name
            or row["machine"] != intent.machine
            or row["native_session_id"] != intent.session_id
            or row["destination_session_id"] != intent.destination_session_id
            or row["destination_machine"] != intent.destination_machine
            or row["target_job_id"] != intent.target_job_id
            or row["native_attempt"] != intent.attempt
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
        call = self._ledger_call(call)
        with self.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            run = self._run(db, lease)
            scopes, expires = self.cap_grant(scopes, expires)
            row = self._row(db, lease, intent)
            if row["call_id"] != call.call_id or row[
                "fingerprint"
            ] != _fingerprint(call.name, call.arguments):
                raise PermissionError(
                    "A replayed tool call cannot change a durable request"
                )
            self._require_parent(
                db,
                run,
                row["kind"],
                row["machine"],
                call.arguments,
                row["destination_machine"],
                row["destination_session_id"],
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
                row["destination_machine"],
            )
            if row["state"] != "reserved":
                return False
            if (
                row["kind"] == "retry"
                and row["native_attempt"]
                != 1
                + self._last_owned_job(
                    db,
                    run,
                    row["machine"],
                    row["native_session_id"],
                    row["target_job_id"],
                )["native_attempt"]
            ):
                raise PermissionError("Native retry reservation is stale")
            db.execute(
                "UPDATE agent_durable_tasks SET state='unknown',updated_at=? WHERE id=?",
                (_now(), intent.id),
            )
            return True

    def reject_retry(self, lease: ActorLease, intent: Intent) -> None:
        """Settle only an explicit native pre-admission refusal, with no receipt."""
        with self.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if lease.database.path != self.database.path:
                raise ActorLeaseLost("Generation actor database mismatch")
            lease.assert_current(db)
            row = self._row(db, lease, intent)
            if (
                row["kind"] != "retry"
                or row["state"] not in {"unknown", "cancel_pending"}
                or row["native_id"] is not None
            ):
                raise PermissionError(
                    "Native retry refusal does not match its reservation"
                )
            db.execute(
                "UPDATE agent_durable_tasks SET state='stopped',updated_at=?,next_attempt_at=NULL,"
                "last_error=? WHERE id=?",
                (_now(), _RETRY_REFUSED, intent.id),
            )

    def bind(self, lease: ActorLease, intent: Intent, native_id: str) -> Intent:
        """Record a trusted native response/lookup with the exact reserved ID."""
        with self.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if lease.database.path != self.database.path:
                raise ActorLeaseLost("Generation actor database mismatch")
            lease.assert_current(db)
            row = self._row(db, lease, intent)
            if (
                native_id != (row["target_job_id"] or row["idempotency_key"])
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
        session_id: str | None = None,
    ) -> bool:
        """Ownership evidence is separate from live native authorization."""
        with self.database.read_snapshot() as db:
            return self._owned(
                db, owner, conversation_id, kind, native_id, machine, session_id
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
            self._cancel_run(db, owner, run_id)

    @staticmethod
    def _cancel_run(db, owner: str, run_id: str) -> None:
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
                row["destination_machine"],
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

    def record_cleanup_receipt(self, identity: str, native_id: str) -> bool:
        """Persist exact positive lookup before a cleanup mutation can lose its reply."""
        with self.database.connect() as db:
            return (
                db.execute(
                    "UPDATE agent_durable_tasks SET native_id=?,updated_at=? "
                    "WHERE id=? AND state='cancel_pending' AND COALESCE(target_job_id,idempotency_key)=? "
                    "AND (native_id IS NULL OR native_id=?)",
                    (native_id, _now(), identity, native_id, native_id),
                ).rowcount
                == 1
            )

    def finish_cancellation(
        self, identity: str, *, stopped: bool, native_confirmed: bool = True
    ) -> None:
        """Only positive native evidence settles a claimed cancellation."""
        now = _now()
        with self.database.connect() as db:
            db.execute(
                "UPDATE agent_durable_tasks SET state=?,updated_at=?,next_attempt_at=?,last_error=?,"
                "native_id=CASE WHEN ? THEN COALESCE(target_job_id,idempotency_key) ELSE native_id END "
                "WHERE id=? AND state='cancel_pending'",
                (
                    "stopped" if stopped else "cancel_pending",
                    now,
                    None if stopped else now + 5_000_000,
                    None if stopped else "Native cancellation unconfirmed",
                    stopped and native_confirmed,
                    identity,
                ),
            )
