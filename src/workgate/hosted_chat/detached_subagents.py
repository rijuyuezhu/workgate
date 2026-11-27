"""Durable detached-child reservations; no runtime or scheduler in this layer."""

import json
import secrets
import time
import uuid
from dataclasses import asdict, dataclass

from .loop_checkpoint import OwnedLoopCheckpoint
from .provider_common import ModelToolCall
from .subagent_scope import SubagentScope
from .subagent_wakeup import register_wakeup
from .subagents import SubagentInvocation

_RUNNING_PER_SCOPE = 10
_RUNNING_GLOBAL = 100
_MAX_THREAD_HISTORY = 512 * 1024


@dataclass(frozen=True)
class DetachedReservation:
    task_id: str
    thread_id: str
    child_conversation_id: str
    status: str


def reserve_detached_child(
    checkpoint: OwnedLoopCheckpoint,
    call: ModelToolCall,
    invocation: SubagentInvocation,
    *,
    disclosure_access: dict | None = None,
) -> DetachedReservation:
    """Bind an exact parent edge before reserving a private child identity.

    A restart between scope admission and this transaction cannot launch a
    child. Replaying the same edge recovers the same reservation and grant.
    """
    thread_id = call.arguments.get("subagent_thread_id")
    if thread_id is not None and (
        not isinstance(thread_id, str) or not thread_id.strip()
    ):
        raise ValueError("Invalid subagent thread ID")
    scope = SubagentScope.admit(
        checkpoint,
        call,
        invocation,
        detached=True,
        disclosure_access=disclosure_access,
    )
    lease = checkpoint.lease
    owner, conversation = lease.owner, invocation.conversation_id
    now = int(time.time() * 1000)
    with lease.database.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        scope.resolve(db, lease)
        existing = db.execute(
            "SELECT * FROM subagent_tasks WHERE owner=? AND parent_conversation_id=? AND idempotency_key=?",
            (owner, conversation, scope.checkpoint_namespace),
        ).fetchone()
        if existing is not None:
            if (
                existing["fingerprint"] != scope.invocation_hash
                or not existing["child_registered"]
            ):
                raise PermissionError("Detached reservation identity changed")
            return DetachedReservation(
                existing["id"],
                existing["thread_id"],
                existing["child_conversation_id"],
                existing["status"],
            )
        counts = db.execute(
            "SELECT count(*) AS total, count(*) FILTER (WHERE status='running') AS live, "
            "count(*) FILTER (WHERE owner=? AND parent_conversation_id=?) AS scoped, "
            "count(*) FILTER (WHERE owner=? AND parent_conversation_id=? AND status='running') AS scoped_live "
            "FROM subagent_tasks",
            (owner, conversation, owner, conversation),
        ).fetchone()
        if (
            counts["scoped_live"] >= _RUNNING_PER_SCOPE
            or counts["live"] >= _RUNNING_GLOBAL
            or counts["scoped"] >= 200
            or counts["total"] >= 2000
        ):
            raise ValueError("Too many detached subagent tasks")
        prior = []
        if thread_id is not None:
            thread = db.execute(
                "SELECT messages FROM subagent_threads WHERE id=? AND owner=? AND parent_conversation_id=? AND subagent_type=?",
                (thread_id, owner, conversation, invocation.subagent_type),
            ).fetchone()
            if thread is None:
                raise PermissionError(
                    "Subagent thread is unavailable in this parent scope"
                )
            if (
                db.execute(
                    "SELECT 1 FROM subagent_tasks WHERE thread_id=? AND status='running'",
                    (thread_id,),
                ).fetchone()
                is not None
            ):
                raise ValueError("Subagent thread is already running")
            latest = db.execute(
                "SELECT * FROM subagent_tasks WHERE thread_id=? ORDER BY rowid DESC LIMIT 1",
                (thread_id,),
            ).fetchone()
            if latest is None or latest["status"] != "completed":
                raise ValueError(
                    "Subagent thread has no completed task to continue"
                )
            previous_options = json.loads(latest["execution_options"])
            previous = SubagentScope.from_record(previous_options["scope"])
            if (
                previous.parent_revision != scope.parent_revision
                or previous.target_revisions != scope.target_revisions
                or previous.parent_scope != scope.parent_scope
                or previous.team_definition_hash != scope.team_definition_hash
                or previous_options.get("disclosure_access")
                != disclosure_access
                or not set(previous.scopes).issubset(scope.scopes)
            ):
                raise PermissionError("Subagent thread authority changed")
            prior = json.loads(thread["messages"])
        else:
            thread_id = "subthread_" + secrets.token_hex(16)
            db.execute(
                "INSERT INTO subagent_threads(id,owner,parent_conversation_id,subagent_type,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                (
                    thread_id,
                    owner,
                    conversation,
                    invocation.subagent_type,
                    now,
                    now,
                ),
            )
        history = json.dumps(
            prior + invocation.messages, ensure_ascii=False, allow_nan=False
        )
        if len(history.encode()) > _MAX_THREAD_HISTORY:
            raise ValueError("Subagent transcript exceeds durable thread limit")
        task_id = "subtask_" + secrets.token_hex(16)
        child_id = str(uuid.uuid4())
        options = json.dumps(
            {
                "scope": scope.record(),
                "disclosure_access": disclosure_access,
                "selected_tools": invocation.selected_tools,
                "recursion_limit": invocation.recursion_limit,
                "remaining_depth": invocation.remaining_depth,
                "child_configs": [
                    asdict(config) for config in invocation.child_configs
                ],
                "ordinary_chat": invocation.ordinary_chat,
            },
            allow_nan=False,
        )
        db.execute(
            "INSERT INTO subagent_tasks(id,owner,parent_conversation_id,parent_run_id,parent_agent_id,parent_tool_call_id,subagent_type,thread_id,idempotency_key,fingerprint,agent_config,execution_options,input,initial_messages,granted_scopes,grant_expires,child_conversation_id,child_registered,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,'running',?,?)",
            (
                task_id,
                owner,
                conversation,
                lease.run_id,
                invocation.parent_agent_id or "default",
                call.call_id,
                invocation.subagent_type,
                thread_id,
                scope.checkpoint_namespace,
                scope.invocation_hash,
                json.dumps(invocation.agent, allow_nan=False),
                options,
                invocation.messages[-1]["content"],
                history,
                json.dumps(scope.scopes),
                scope.grant_expires,
                child_id,
                now,
                now,
            ),
        )
        db.execute(
            "INSERT INTO active_subagent_threads(owner,thread_id,parent_run_id,active) VALUES(?,?,?,1)",
            (owner, child_id, lease.run_id),
        )
        register_wakeup(db, task_id, scope)
        return DetachedReservation(task_id, thread_id, child_id, "running")
