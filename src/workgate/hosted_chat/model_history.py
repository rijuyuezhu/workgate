"""Canonical terminal model suffix, adapted from upstream v6.0.3 message_dto.py."""

import copy
import hashlib
import json
import sqlite3
import time

from .checkpoint_types import LoopSnapshot


def history_boundary(history: list[dict]) -> dict:
    encoded = json.dumps(
        history, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    return {
        "count": len(history),
        "sha256": hashlib.sha256(encoded.encode()).hexdigest(),
    }


def completed_model_steps(
    history: list[dict], messages: list[dict], *, interrupted: bool = False
) -> list[dict]:
    """Take complete ordered exchanges, never orphaned calls or UI previews.

    Interrupted turns keep only the maximal complete prefix. A successful turn
    must validate in its entirety; neither mode may change the admitted prefix.
    """
    start = len(history)
    if messages[:start] != history:
        raise ValueError("Canonical model history prefix changed")
    steps = messages[start:]
    pending = set()
    boundary = 0

    def incomplete(error: str) -> list[dict]:
        if not interrupted:
            raise ValueError(error)
        return copy.deepcopy(steps[:boundary])

    for index, item in enumerate(steps, 1):
        role = item.get("role")
        if role == "tool":
            ident = item.get("tool_call_id")
            if not isinstance(ident, str) or ident not in pending:
                return incomplete(
                    "Canonical model step has no tool declaration"
                )
            pending.remove(ident)
        else:
            if pending:
                return incomplete("Canonical model tool batch is incomplete")
            if role == "assistant":
                calls = item.get("tool_calls", [])
                if not isinstance(calls, list):
                    return incomplete("Invalid canonical tool batch")
                for call in calls:
                    ident = call.get("id") if isinstance(call, dict) else None
                    if (
                        not isinstance(ident, str)
                        or not ident
                        or ident in pending
                    ):
                        return incomplete(
                            "Invalid canonical tool call identity"
                        )
                    pending.add(ident)
            elif role != "user" or item.get("source") != "steer":
                return incomplete(
                    "Unexpected message in canonical model suffix"
                )
        if not pending:
            boundary = index
    if pending:
        return incomplete("Canonical model tool batch is incomplete")
    return copy.deepcopy(steps)


def recover_interrupted_model_steps(
    db: sqlite3.Connection, row, state: dict
) -> list[dict] | None:
    """Read the exact public root checkpoint within the terminal transaction.

    The caller already holds the root transition authority. No lease recovery,
    provider calls, or tool execution is permitted during history recovery.
    """
    boundary = state.get("modelHistoryBoundary")
    if (
        state.get("ownedLoopCheckpoint") is not True
        or not isinstance(boundary, dict)
        or state.get("manualCompaction")
        or state.get("completionWakeupTaskId")
        or state.get("detachedTaskId")
        or type(boundary.get("count")) is not int
        or boundary["count"] < 0
    ):
        return None
    try:
        checkpoint = db.execute(
            "SELECT a.checkpoint,a.metadata FROM agent_checkpoints a "
            "JOIN conversations c ON c.owner=a.owner AND c.id=a.thread_id "
            "WHERE c.id=? AND a.checkpoint_ns=? AND a.expires_at>? "
            "AND a.checkpoint IS NOT NULL ORDER BY a.checkpoint_id DESC LIMIT 1",
            (row["conversation_id"], row["id"], time.time()),
        ).fetchone()
    except sqlite3.OperationalError as error:
        if "no such table: agent_checkpoints" in str(error):
            return None
        raise
    if checkpoint is None:
        return None
    try:
        value = json.loads(checkpoint["checkpoint"])
        if (
            value.get("v") != 1
            or json.loads(checkpoint["metadata"]).get("source") != "loop"
        ):
            return None
        snapshot = LoopSnapshot.model_validate(value["channel_values"])
        snapshot.calls()
        count = boundary["count"]
        if snapshot.agent_id != row["agent_id"] or count > len(
            snapshot.messages
        ):
            return None
        prefix = snapshot.messages[:count]
        if history_boundary(prefix) != boundary:
            return None
        return (
            completed_model_steps(prefix, snapshot.messages, interrupted=True)
            or None
        )
    except ValueError, KeyError, TypeError, AttributeError:
        # Damaged or legacy checkpoints cannot prevent terminal settlement.
        return None
