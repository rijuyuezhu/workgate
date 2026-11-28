"""Persist root provider-only context fading under the active generation lease."""

import json
from collections.abc import Callable

from .agent_runtime import AgentLoopState
from .context_budget import calculate_budget
from .context_calibration import TokenCalibration
from .context_usage import _context_options
from .fading import resolve_fading_tier, seed_fading_tier
from .generation_actor_lease import ActorLease


def update_root_fading(
    lease: ActorLease,
    state: AgentLoopState,
    messages: list[dict],
    *,
    check_grant: Callable[[], object],
) -> dict | None:
    """Resolve a v2 tier for this exact root model and admitted branch.

    Only the previous completed response on the current branch may seed a
    new run. A committed compaction resets the existing hysteresis. No
    provider-facing projection is stored as canonical message history.
    """
    if lease.detached_task_id is not None or lease.wakeup_task_id is not None:
        return None
    check_grant()
    with lease.database.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        lease.assert_current(db)
        row = db.execute(
            "SELECT * FROM runs WHERE id=?", (lease.run_id,)
        ).fetchone()
        run = json.loads(row["state"])
        profile = state.context.profile
        if (
            run.get("manualCompaction")
            or run["provider"] != (profile.channel or profile.provider)
            or run["model"] != profile.model
            or row["agent_id"] != (state.context.agent or {}).get("id")
        ):
            return None
        limit, reserve = _context_options(db, lease.owner, state)
        if limit is None:
            return None
        identity = [
            row["agent_id"],
            run.get("agentRevision"),
            run["provider"],
            run["model"],
        ]
        current = run.get("contextFading")
        compaction = run.get("compaction")
        checkpoint = (
            compaction.get("fingerprint")
            if isinstance(compaction, dict)
            else None
        )
        seed = None
        if isinstance(current, dict) and current.get("identity") == identity:
            if current.get("compaction") == checkpoint:
                seed = current.get("tier")
        elif checkpoint is None:
            parent = db.execute(
                "SELECT parent_id FROM messages WHERE id=? AND conversation_id=?",
                (run["userMessageId"], row["conversation_id"]),
            ).fetchone()
            if parent is not None and parent["parent_id"] is not None:
                previous = db.execute(
                    "SELECT state FROM runs WHERE conversation_id=? AND status='complete' "
                    "AND json_extract(state,'$.responseMessageId')=?",
                    (row["conversation_id"], parent["parent_id"]),
                ).fetchall()
                if len(previous) == 1:
                    prior = json.loads(previous[0]["state"]).get(
                        "contextFading"
                    )
                    if (
                        isinstance(prior, dict)
                        and prior.get("identity") == identity
                    ):
                        seed = prior.get("tier")
        settings_row = db.execute(
            "SELECT value FROM settings WHERE owner=? AND key='endpoints'",
            (lease.owner,),
        ).fetchone()
        settings = json.loads(settings_row[0]) if settings_row else {}
        config = (
            settings.get("summarization")
            if isinstance(settings, dict)
            else None
        )
        enabled = isinstance(config, dict) and config.get("enabled") is True
        calibration = TokenCalibration.from_dict(run.get("contextCalibration"))
        budget = calculate_budget(
            messages,
            state.context.tools,
            limit,
            reserve_ratio=reserve,
            calibration_ratio=calibration.ratio,
        )
        width = 1
        for message in reversed(state.messages):
            if message.get("role") == "user":
                break
            width = max(width, len(message.get("tool_calls") or []))
        pressure = (
            limit - budget.reserve_tokens - budget.remaining_context_tokens
        ) / max(1, limit - budget.reserve_tokens)
        tier = resolve_fading_tier(
            seed_fading_tier(limit, seed),
            limit,
            {
                "contextPressure": pressure,
                "effectiveRawTokens": max(
                    0, budget.available_for_messages / calibration.ratio
                ),
                "summarizationEnabled": enabled,
                "toolExchangeWidth": width,
            },
        )
        record = {"identity": identity, "compaction": checkpoint, "tier": tier}
        if current != record:
            run["contextFading"] = record
            db.execute(
                "UPDATE runs SET state=? WHERE id=?",
                (
                    json.dumps(run, ensure_ascii=False, separators=(",", ":")),
                    lease.run_id,
                ),
            )
        check_grant()
        lease.assert_current(db)
        return tier
