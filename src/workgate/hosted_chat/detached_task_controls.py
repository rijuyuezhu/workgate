"""Private owner-scoped background task controls at a committed parent edge."""

import json
import secrets
import time
from dataclasses import asdict
from datetime import UTC, datetime

from .checkpoint_types import LoopSnapshot
from .detached_task_lease import _now, _request_cancel_locked
from .loop_checkpoint import OwnedLoopCheckpoint
from .provider_common import ModelToolCall
from .subagent_scope import SubagentScope, _digest

CHECK_BACKGROUND_TASK = "check_background_task"


def _snapshot(row, *, status=None) -> dict:
    running = row["status"] == "running"
    end = _now() if running else max(row["created_at"], row["updated_at"])
    result = {
        "background_task_id": row["id"],
        "subagent_thread_id": row["thread_id"],
        "tool": "subagent",
        "subagent_type": row["subagent_type"],
        "status": status or row["status"],
        "progress": int(not running),
        "started_at": datetime.fromtimestamp(
            row["created_at"] / 1000, UTC
        ).isoformat(),
        "elapsed_ms": max(0, end - row["created_at"]),
    }
    if not running:
        result["settled_at"] = datetime.fromtimestamp(
            end / 1000, UTC
        ).isoformat()
        result["result_available"] = not bool(row["result_claimed"])
        result["result_claimed"] = bool(row["result_claimed"])
    return result


def _authorize_result(db, row, scopes, expires, disclosure_access):
    options = json.loads(row["execution_options"])
    scope = SubagentScope.from_record(options["scope"])
    if (
        not scope.detached
        or scope.checkpoint_namespace != row["idempotency_key"]
        or scope.invocation_hash != row["fingerprint"]
        or scope.run_id != row["parent_run_id"]
        or options.get("disclosure_access") != disclosure_access
        or list(scope.scopes) != json.loads(row["granted_scopes"])
        or scope.grant_expires != row["grant_expires"]
    ):
        raise PermissionError("Detached result authority changed")
    scope.resolve_owned(db, row["owner"], row["parent_run_id"])
    scope.effective_grant(scopes, expires)


def check_detached_task(
    checkpoint: OwnedLoopCheckpoint,
    call: ModelToolCall,
    *,
    scopes: set[str],
    expires: int | None,
    disclosure_access: dict | None = None,
) -> dict:
    """List, poll or cancel without starting a worker; claim a result once.

    The caller supplies a current grant. Only a root parent tool edge is
    supported here; graph-member and child control authority are not inferred.
    """
    if checkpoint.scope is not None or call.name != CHECK_BACKGROUND_TASK:
        raise PermissionError(
            "Background task control requires a root parent edge"
        )
    if expires is not None and expires <= time.time():
        raise PermissionError("Authentication grant expired")
    args = call.arguments
    if set(args) - {"background_task_id", "action", "message", "control_id"}:
        raise ValueError("Unsupported background task control arguments")
    action = args.get("action", "poll")
    task_id = args.get("background_task_id")
    if action not in (
        "poll",
        "cancel",
        "steer",
        "queue",
        "interrupt",
        "cancel_message",
    ):
        raise ValueError("Unsupported background task control action")
    if task_id is not None and (
        not isinstance(task_id, str)
        or not task_id.strip()
        or len(task_id) > 256
    ):
        raise ValueError("Invalid background task ID")
    if action != "poll" and task_id is None:
        raise ValueError("Control requires a background task ID")
    message = args.get("message")
    control_id = args.get("control_id")
    if action in ("steer", "queue", "interrupt"):
        if (
            not isinstance(message, str)
            or not message.strip()
            or len(message) > 65536
        ):
            raise ValueError(
                "A non-empty control message of at most 65536 characters is required"
            )
    elif message is not None:
        raise ValueError("Unexpected control message")
    if action == "cancel_message":
        if not isinstance(control_id, str) or not 0 < len(control_id) <= 256:
            raise ValueError("A control ID is required")
    elif control_id is not None:
        raise ValueError("Unexpected control ID")
    lease = checkpoint.lease
    with lease.database.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        lease.assert_current(db)
        run = db.execute(
            "SELECT conversation_id,agent_id FROM runs WHERE id=?",
            (lease.run_id,),
        ).fetchone()
        conversation = run["conversation_id"]
        agent_id = run["agent_id"] or "default"
        edge = db.execute(
            "SELECT checkpoint_id,checkpoint,metadata FROM agent_checkpoints WHERE owner=? AND thread_id=? AND checkpoint_ns=? AND expires_at>? AND checkpoint IS NOT NULL ORDER BY checkpoint_id DESC LIMIT 1",
            (lease.owner, conversation, lease.run_id, time.time()),
        ).fetchone()
        if edge is None or edge["checkpoint_id"] != checkpoint.active[
            "configurable"
        ].get("checkpoint_id"):
            raise PermissionError(
                "Background task tool edge is no longer current"
            )
        saved = json.loads(edge["checkpoint"])
        if (
            saved.get("v") != 1
            or json.loads(edge["metadata"]).get("source") != "loop"
        ):
            raise PermissionError(
                "Background task requires a committed loop edge"
            )
        snapshot = LoopSnapshot.model_validate(saved["channel_values"])
        if (
            not snapshot.tool_step_started
            or snapshot.agent_id != run["agent_id"]
            or call not in snapshot.calls()
        ):
            raise PermissionError(
                "Background task proposal is not the started parent edge"
            )
        fingerprint = _digest([edge["checkpoint_id"], asdict(call)])
        key = (lease.owner, conversation, lease.run_id, agent_id, call.call_id)
        prior = db.execute(
            "SELECT * FROM subagent_task_invocations WHERE owner=? AND parent_conversation_id=? AND parent_run_id=? AND parent_agent_id=? AND tool_call_id=?",
            key,
        ).fetchone()
        rows = db.execute(
            "SELECT * FROM subagent_tasks WHERE owner=? AND parent_conversation_id=? AND parent_agent_id=? AND json_type(execution_options,'$.scope.parent_scope')='null' AND (? IS NULL OR id=?) ORDER BY rowid",
            (lease.owner, conversation, agent_id, task_id, task_id),
        ).fetchall()
        if prior is not None:
            if (
                prior["fingerprint"] != fingerprint
                or prior["status"] != "completed"
            ):
                raise PermissionError(
                    "Background task invocation identity changed"
                )
            output = json.loads(prior["result"])
            if "result" in output or "error" in output:
                if not rows:
                    raise PermissionError(
                        "Background task result no longer owned"
                    )
                _authorize_result(
                    db, rows[0], scopes, expires, disclosure_access
                )
            return output
        if task_id is None:
            output = {
                "tasks": [_snapshot(row) for row in rows],
                "outstanding": sum(row["status"] == "running" for row in rows),
            }
        elif not rows:
            output = {"status": "not_found", "background_task_id": task_id}
        else:
            row = rows[0]
            if action in ("steer", "queue", "interrupt", "cancel_message"):
                if row["status"] != "running" or row["cancel_requested"]:
                    output = _snapshot(row, status="not_running")
                elif (
                    json.loads(row["execution_options"])["scope"].get(
                        "team_definition_hash"
                    )
                    is not None
                ):
                    output = _snapshot(row, status="controls_not_enabled")
                else:
                    _authorize_result(
                        db, row, scopes, expires, disclosure_access
                    )
                    if action == "cancel_message":
                        changed = db.execute(
                            "UPDATE subagent_task_controls SET status='rejected',reason='withdrawn',updated_at=? WHERE task_id=? AND id=? AND status='accepted' AND boundary IS NULL",
                            (_now(), task_id, control_id),
                        ).rowcount
                        output = _snapshot(
                            row,
                            status="accepted"
                            if changed
                            else "control_not_found",
                        )
                        output["control_id"] = control_id
                    else:
                        pending = db.execute(
                            "SELECT count(*) FROM subagent_task_controls WHERE task_id=? AND status='accepted'",
                            (task_id,),
                        ).fetchone()[0]
                        if pending >= 32:
                            raise ValueError(
                                "Task already has 32 pending messages"
                            )
                        assert isinstance(message, str)
                        control_id = "control_" + secrets.token_hex(16)
                        now = _now()
                        db.execute(
                            "INSERT INTO subagent_task_controls(id,task_id,invocation_key,action,message,status,created_at,updated_at) VALUES(?,?,?,?,?,'accepted',?,?)",
                            (
                                control_id,
                                task_id,
                                fingerprint,
                                action,
                                message.strip(),
                                now,
                                now,
                            ),
                        )
                        output = _snapshot(row, status="accepted")
                        output["control_id"] = control_id
            elif action == "cancel" and row["status"] == "running":
                _request_cancel_locked(lease.database, db, row)
                row = db.execute(
                    "SELECT * FROM subagent_tasks WHERE id=?", (task_id,)
                ).fetchone()
                output = _snapshot(
                    row,
                    status="cancelled"
                    if row["status"] == "cancelled"
                    else "cancellation_requested",
                )
            elif action == "cancel":
                output = _snapshot(row, status="not_running")
            elif row["status"] == "running":
                output = _snapshot(row)
            elif row["result_claimed"]:
                output = _snapshot(
                    row,
                    status="delivery_scheduled"
                    if row["result_claim_key"] == "wakeup:" + task_id
                    else "claimed",
                )
            else:
                _authorize_result(db, row, scopes, expires, disclosure_access)
                db.execute(
                    "UPDATE subagent_wakeups SET status='retired',error='Manually claimed',updated_at=? WHERE task_id=? AND status='pending'",
                    (_now(), task_id),
                )
                db.execute(
                    "UPDATE subagent_tasks SET result_claimed=1,result_claim_key=?,updated_at=? WHERE id=?",
                    ("poll:" + fingerprint, _now(), task_id),
                )
                output = _snapshot(row)
                output.update(result_available=False, result_claimed=True)
                if row["status"] == "completed":
                    output["result"] = row["result"]
                elif row["error"] is not None:
                    output["error"] = row["error"]
        now = _now()
        db.execute(
            "INSERT INTO subagent_task_invocations(owner,parent_conversation_id,parent_run_id,parent_agent_id,tool_call_id,fingerprint,status,result,created_at,updated_at) VALUES(?,?,?,?,?,?,'completed',?,?,?)",
            (*key, fingerprint, json.dumps(output, allow_nan=False), now, now),
        )
        return output
