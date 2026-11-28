"""Apply an explicitly enabled automatic Memory Agent's bounded batch atomically."""

import hashlib
import json
import time
from collections.abc import Callable
from dataclasses import asdict

from .content_filters import filters_from_snapshot, validate_tool_arguments
from .credential_cipher import CredentialCipher
from .generation_actor_lease import ActorLease
from .generation_protocol import EVENT_LIMIT
from .memory import MemoryRepository
from .memory_extraction import _MAX_PROPOSALS, MemoryProposals, _plan_snapshot
from .memory_protection import assert_memory_content_allowed
from .memory_tools import validate_call
from .provider_common import complete_tool_batch
from .provider_profiles import ProviderRepository
from .providers import provider_config


def apply_memory_proposals(
    lease: ActorLease,
    proposals: MemoryProposals,
    *,
    expires: int | None,
    check_grant: Callable[[], None],
) -> dict:
    """Commit one entire proposal batch, receipt, and value-free event or none.

    The separately configured automatic agent is the owner's opt-in. This
    function never grants access to native tools or creates execution tasks.
    """
    if lease.detached_task_id is not None or lease.wakeup_task_id is not None:
        raise PermissionError("Automatic memory requires an interactive root")
    check_grant()
    calls = complete_tool_batch(list(proposals.calls))
    if len(calls) > _MAX_PROPOSALS:
        raise ValueError("Memory extraction proposal limit reached")
    fingerprint = hashlib.sha256(
        json.dumps(
            [proposals.plan.fingerprint, [asdict(call) for call in calls]],
            sort_keys=True,
            allow_nan=False,
        ).encode()
    ).hexdigest()
    with lease.database.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        lease.assert_current(db)
        check_grant()
        if expires is not None and expires <= time.time():
            raise PermissionError("Automatic memory grant expired")
        row = db.execute(
            "SELECT state,conversation_id FROM runs WHERE id=?", (lease.run_id,)
        ).fetchone()
        state = json.loads(row["state"])
        prior = state.get("automaticMemoryReceipt")
        if prior is not None:
            if prior["fingerprint"] != fingerprint:
                raise PermissionError("Automatic memory batch already settled")
            return prior["result"]
        admission = _plan_snapshot(db, lease)
        if admission is None:
            raise PermissionError("Automatic memory is no longer admitted")
        provider, model, _, keys, partition, identity = admission
        if (
            identity != proposals.plan.fingerprint
            or partition != proposals.plan.partition
        ):
            raise PermissionError("Automatic memory admission changed")
        # The immediate transaction serializes channel changes along with
        # settings and memory writes. Resolve the current encrypted profile
        # through the repository, never through a model-supplied credential.
        repository = ProviderRepository(
            lease.database, CredentialCipher(lease.database)
        )
        if (
            provider_config(repository, provider, model, allow_account=False)
            != proposals.plan.profile
        ):
            raise PermissionError("Automatic memory model channel changed")
        filters = filters_from_snapshot(db, lease.owner)
        seen = set()
        for call in calls:
            validate_call(call)
            key = call.arguments["key"]
            if key in seen or (keys and key not in keys):
                raise ValueError("Invalid or repeated automatic memory key")
            seen.add(key)
            validate_tool_arguments(call.name, call.arguments, filters)
            assert_memory_content_allowed(
                key, call.arguments.get("value", ""), filters
            )
        updated = deleted = 0
        memory = MemoryRepository(lease.database)
        for call in calls:
            key = call.arguments["key"]
            if call.name == "set_memory":
                memory.write(
                    lease.owner,
                    key,
                    call.arguments["value"],
                    agent_id=partition,
                    _connection=db,
                )
                updated += 1
            else:
                deleted += db.execute(
                    "DELETE FROM webui_memories WHERE owner=? AND agent_id=? AND key=?",
                    (lease.owner, partition, key),
                ).rowcount
        result = {"updated": updated, "deleted": deleted}
        state["automaticMemoryReceipt"] = {
            "fingerprint": fingerprint,
            "result": result,
        }
        db.execute(
            "UPDATE runs SET state=? WHERE id=?",
            (json.dumps(state, separators=(",", ":")), lease.run_id),
        )
        if updated or deleted:
            count = db.execute(
                "SELECT count(*) FROM generation_events WHERE run_id=?",
                (lease.run_id,),
            ).fetchone()[0]
            if count >= EVENT_LIMIT:
                raise ValueError("Generation event retention limit reached")
            event = {
                "event": "on_memory_update",
                "data": {
                    "conversationId": row["conversation_id"],
                    "runId": lease.run_id,
                    **result,
                },
            }
            db.execute(
                "INSERT INTO generation_events(run_id,data) VALUES(?,?)",
                (lease.run_id, json.dumps(event, separators=(",", ":"))),
            )
        check_grant()
        lease.assert_current(db)
        if expires is not None and expires <= time.time():
            raise PermissionError("Automatic memory grant expired")
        return result
