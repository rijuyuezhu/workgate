"""Shared source/target identity checks for event admission and actor leases."""

import json

from .event_bindings import live_binding
from .event_source_keys import live_source


def live_event(db, owner: str, delivery_id: str):
    row = db.execute(
        "SELECT * FROM agent_event_deliveries WHERE id=? AND owner=? AND batch_root_id IS NULL",
        (delivery_id, owner),
    ).fetchone()
    if row is None or not live_source(
        db, owner, row["source_key_id"], mode=row["mode"]
    ):
        return None
    if row["mode"] not in {"fire", "continue"}:
        return None
    envelope = json.loads(row["envelope"])
    target = envelope["target"]
    if (
        envelope["mode"] != row["mode"]
        or envelope["principal"] != {"userId": owner}
        or envelope["event"]["source"]["id"] != row["source_key_id"]
    ):
        return None
    if not db.execute(
        "SELECT 1 FROM agents WHERE id=? AND owner=?",
        (target["agentId"], owner),
    ).fetchone():
        return None
    if row["mode"] == "continue":
        binding = live_binding(
            db, owner, row["source_key_id"], target["bindingId"]
        )
        if binding is None or (
            binding["agent_id"],
            binding["conversation_id"],
        ) != (target["agentId"], target["conversationId"]):
            return None
    if row["run_id"] is not None:
        run = db.execute(
            "SELECT r.state,r.agent_id,r.conversation_id FROM runs r JOIN conversations c ON c.id=r.conversation_id WHERE r.id=? AND c.owner=?",
            (row["run_id"], owner),
        ).fetchone()
        if run is None:
            return None
        state = json.loads(run["state"])
        if (
            state.get("eventDeliveryId") != delivery_id
            or state.get("eventSourceKeyId") != row["source_key_id"]
            or state.get("nativeAdmission") is not None
            or state.get("scheduleOccurrenceId") is not None
            or state.get("completionWakeupTaskId") is not None
            or run["agent_id"] != target["agentId"]
            or (
                row["mode"] == "continue"
                and run["conversation_id"] != target["conversationId"]
            )
        ):
            return None
    return row
