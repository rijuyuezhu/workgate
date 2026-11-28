"""Root foreground memory tools with atomic approval-edge receipts."""

import hashlib
import json
import time
from dataclasses import asdict

from .approval_claims import ApprovalClaimStore
from .approval_runtime import prepare_approval_batch
from .checkpoint_types import LoopSnapshot
from .content_filters import filters_from_snapshot
from .loop_checkpoint import OwnedLoopCheckpoint
from .memory import (
    MemoryRepository,
    _memory_config_snapshot,
    agent_partition,
    tool_definitions,
)
from .memory_protection import assert_memory_content_allowed
from .provider_common import ModelToolCall

MEMORY_TOOLS = frozenset({"set_memory", "delete_memory"})


def _settings(db, owner, key):
    row = db.execute(
        "SELECT value FROM settings WHERE owner=? AND key=?", (owner, key)
    ).fetchone()
    return json.loads(row[0]) if row else None


def _context(db, checkpoint: OwnedLoopCheckpoint):
    lease = checkpoint.lease
    lease.assert_current(db)
    if (
        checkpoint.scope is not None
        or lease.detached_task_id is not None
        or lease.wakeup_task_id is not None
    ):
        raise PermissionError("Memory mutation requires an interactive root")
    row = db.execute(
        "SELECT r.* FROM runs r JOIN conversations c ON c.id=r.conversation_id WHERE r.id=? AND c.owner=?",
        (lease.run_id, lease.owner),
    ).fetchone()
    state = json.loads(row["state"])
    agent = None
    if row["agent_id"] is not None:
        saved = db.execute(
            "SELECT config FROM agents WHERE owner=? AND id=?",
            (lease.owner, row["agent_id"]),
        ).fetchone()
        if saved is None:
            raise PermissionError("Memory Agent is unavailable")
        agent = {**json.loads(saved["config"]), "id": row["agent_id"]}
        if agent.get("_version", 1) != state.get(
            "agentRevision"
        ) or "memory" not in (agent.get("tools") or []):
            raise PermissionError("Memory Agent selection changed")
    config = _memory_config_snapshot(db, lease.owner)
    if config is None or _settings(db, lease.owner, "user.memories") is False:
        raise PermissionError("Memory is disabled")
    enabled = True
    for name in (
        "conversation.features.global",
        "conversation.permissions." + row["conversation_id"],
    ):
        value = _settings(db, lease.owner, name)
        if isinstance(value, dict) and "memory" in value:
            enabled = value["memory"] is True
    endpoints = _settings(db, lease.owner, "endpoints")
    capabilities = (
        (endpoints.get("agents") or {}).get("capabilities")
        if isinstance(endpoints, dict)
        and isinstance(endpoints.get("agents"), dict)
        else None
    )
    if not enabled or (
        isinstance(capabilities, list) and "memory" not in capabilities
    ):
        raise PermissionError("Memory capability is disabled")
    valid = config.get("validKeys")
    if valid is not None and (
        not isinstance(valid, list)
        or len(valid) > 1000
        or any(not isinstance(key, str) for key in valid)
    ):
        raise ValueError("Invalid memory key configuration")
    return state, agent_partition(agent), valid


def definitions(checkpoint: OwnedLoopCheckpoint) -> list[dict]:
    with checkpoint.lease.database.read_snapshot() as db:
        try:
            _, _, valid = _context(db, checkpoint)
        except PermissionError:
            return []
        return tool_definitions(valid)


def validate_call(call: ModelToolCall) -> None:
    if call.name not in MEMORY_TOOLS:
        raise ValueError("Unknown memory tool")
    expected = {"key", "value"} if call.name == "set_memory" else {"key"}
    if set(call.arguments) != expected or any(
        not isinstance(value, str) for value in call.arguments.values()
    ):
        raise ValueError("Invalid memory arguments")
    MemoryRepository._check(call.arguments["key"], call.arguments.get("value"))


def execute_memory_call(
    checkpoint: OwnedLoopCheckpoint,
    call: ModelToolCall,
    *,
    expires: int | None,
    replay_only: bool = False,
) -> dict:
    """Validate the committed original approval and store only a receipt digest.

    The mutation and receipt share the generation transaction, including the
    live actor lease, Agent revision, opt-out, quota, and content policy.
    """
    validate_call(call)
    lease = checkpoint.lease
    with lease.database.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        if expires is not None and expires <= time.time():
            raise PermissionError("Memory grant expired")
        state, partition, valid = _context(db, checkpoint)
        identity = checkpoint.active["configurable"].get("checkpoint_id")
        if not isinstance(identity, str):
            raise PermissionError("Memory tool checkpoint missing")
        edge = ApprovalClaimStore._edge(
            db, checkpoint.saver, checkpoint.base, identity
        )
        snapshot = LoopSnapshot.model_validate(
            edge["checkpoint"]["channel_values"]
        )
        if call not in snapshot.calls():
            raise PermissionError("Memory call differs from original proposal")
        claim = state.get("approvedToolBatch")
        if not isinstance(claim, dict) or claim.get("checkpointId") != identity:
            raise PermissionError("Memory requires explicit human approval")
        ApprovalClaimStore._verify_requests(
            edge, claim["payload"], allow_settled=True
        )
        batch = prepare_approval_batch(claim["payload"], claim["resolutions"])
        entry = next(
            (item for item in batch.entries if item.call_id == call.call_id),
            None,
        )
        if (
            entry is None
            or entry.decision["type"] != "approve"
            or entry.tool_call().name != call.name
            or entry.tool_call().arguments != call.arguments
        ):
            raise PermissionError("Memory mutation was not approved exactly")
        key = call.arguments["key"]
        if valid and key not in valid:
            raise ValueError("Memory key is not allowed")
        assert_memory_content_allowed(
            key,
            call.arguments.get("value", ""),
            filters_from_snapshot(db, lease.owner),
        )
        fingerprint = hashlib.sha256(
            json.dumps(
                [identity, partition, asdict(call)],
                sort_keys=True,
                allow_nan=False,
            ).encode()
        ).hexdigest()
        receipts = state.setdefault("memoryMutationReceipts", {})
        prior = receipts.get(call.call_id)
        if prior is not None:
            if prior["fingerprint"] != fingerprint:
                raise PermissionError("Memory invocation identity changed")
            return prior["result"]
        if replay_only:
            raise PermissionError("Memory mutation receipt missing")
        if len(receipts) >= 256:
            raise OverflowError("Memory invocation limit reached")
        if call.name == "set_memory":
            MemoryRepository(lease.database).write(
                lease.owner,
                key,
                call.arguments["value"],
                agent_id=partition,
                _connection=db,
            )
            result = {"content": [{"type": "text", "text": "Memory saved."}]}
        else:
            deleted = db.execute(
                "DELETE FROM webui_memories WHERE owner=? AND agent_id=? AND key=?",
                (lease.owner, partition, key),
            ).rowcount
            result = {
                "content": [
                    {
                        "type": "text",
                        "text": "Memory deleted."
                        if deleted
                        else "Memory not found.",
                    }
                ],
                "isError": not bool(deleted),
            }
        receipts[call.call_id] = {"fingerprint": fingerprint, "result": result}
        db.execute(
            "UPDATE runs SET state=? WHERE id=?",
            (json.dumps(state, separators=(",", ":")), lease.run_id),
        )
        return result
