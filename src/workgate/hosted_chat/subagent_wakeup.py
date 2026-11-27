"""Internal child-completion envelope, adapted from pinned subagent_wakeup.py.

Registration shares the child reservation transaction. An envelope is routing
metadata, never an authorization grant or a public event submission.
"""

import json
import sqlite3
import uuid

from .subagent_scope import SubagentScope

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
