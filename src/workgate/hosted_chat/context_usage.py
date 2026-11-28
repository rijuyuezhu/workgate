"""Persist owner-bound confirmed usage and estimated provider context budgets."""

import json
import sqlite3

from .agent_runtime import AgentLoopState
from .context_budget import calculate_budget, provider_usage_snapshot
from .context_calibration import MAX_CUMULATIVE_TOKENS, TokenCalibration
from .generation_actor_lease import ActorLease
from .generation_protocol import EVENT_LIMIT, MAX_EVENT_BYTES
from .provider_common import ModelUsage
from .provider_profiles import ProviderRepository


def _bounded_limit(value: object) -> int | None:
    if (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and 1 <= value <= 2_000_000
    ):
        return int(value)
    return None


def _context_options(
    db: sqlite3.Connection, owner: str, state: AgentLoopState
) -> tuple[int | None, float]:
    row = db.execute(
        "SELECT value FROM settings WHERE owner=? AND key='endpoints'", (owner,)
    ).fetchone()
    settings = json.loads(row[0]) if row else {}
    if not isinstance(settings, dict):
        settings = {}
    agents = settings.get("agents")
    params = state.context.parameters
    limit = _bounded_limit(
        params.get("max_context_tokens") or params.get("maxContextTokens")
    )
    if limit is None and isinstance(agents, dict):
        limit = _bounded_limit(agents.get("maxContextTokens"))
    if limit is None:
        profile = state.context.profile
        stored = ProviderRepository._profile_record(
            db, profile.channel or profile.provider
        )
        limit = _bounded_limit(
            stored.get("model_contexts", {}).get(profile.model)
        )
    options = settings.get("summarization")
    reserve = (
        options.get("reserveRatio", 0.05) if isinstance(options, dict) else 0.05
    )
    if type(reserve) not in (int, float) or not 0 <= reserve < 1:
        reserve = 0.05
    return limit, float(reserve)


def record_root_context_usage(
    lease: ActorLease, state: AgentLoopState, usage: ModelUsage
) -> bool:
    """Record one confirmed usage report per durable root model turn.

    A crash can replay a turn before its checkpoint is sealed. Do not count
    that turn twice. Private children and wakeups never publish root metrics.
    """
    if lease.detached_task_id is not None or lease.wakeup_task_id is not None:
        return False
    profile = state.context.profile
    if (
        usage.provider != profile.provider
        or type(state.model_turn) is not int
        or state.model_turn < 1
        or type(usage.input_tokens) is not int
        or not 0 < usage.input_tokens <= MAX_CUMULATIVE_TOKENS
        or type(usage.output_tokens) is not int
        or not 0 <= usage.output_tokens <= MAX_CUMULATIVE_TOKENS
        or state.provider_projection is None
    ):
        return False
    with lease.database.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        lease.assert_current(db)
        row = db.execute(
            "SELECT state,agent_id FROM runs WHERE id=?", (lease.run_id,)
        ).fetchone()
        run = json.loads(row["state"])
        if (
            run["provider"] != (profile.channel or profile.provider)
            or run["model"] != profile.model
            or row["agent_id"] != (state.context.agent or {}).get("id")
            or state.model_turn <= run.get("contextUsageTurn", 0)
        ):
            return False
        messages = state.provider_projection
        tools = state.context.tools
        limit, reserve = _context_options(db, lease.owner, state)
        calibration = TokenCalibration.from_dict(run.get("contextCalibration"))
        raw = calculate_budget(messages, tools, 2_000_000, reserve_ratio=0)
        updated = calibration.observe(
            provider_input_tokens=usage.input_tokens,
            raw_sent_this_turn=raw.message_tokens,
            instruction_overhead=raw.instruction_tokens
            + raw.tool_schema_tokens,
        )
        snapshot = provider_usage_snapshot(
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            provider=usage.provider,
            model=profile.model,
            messages=messages,
            tools=tools,
            max_context_tokens=limit,
            run_id=lease.run_id,
            agent_id=row["agent_id"],
            reserve_ratio=reserve,
            calibration_ratio=calibration.ratio,
        )
        event = json.dumps(
            {"event": "on_context_usage", "data": snapshot},
            separators=(",", ":"),
            allow_nan=False,
        )
        if len(event.encode()) > MAX_EVENT_BYTES:
            raise ValueError("Context usage event exceeds retention limit")
        count = db.execute(
            "SELECT count(*) FROM generation_events WHERE run_id=?",
            (lease.run_id,),
        ).fetchone()[0]
        if count >= EVENT_LIMIT:
            raise ValueError("Generation event retention limit reached")
        run.update(
            contextUsage=snapshot,
            contextUsageTurn=state.model_turn,
            contextCalibration=updated.to_dict(),
        )
        db.execute(
            "UPDATE runs SET state=? WHERE id=?",
            (
                json.dumps(run, ensure_ascii=False, separators=(",", ":")),
                lease.run_id,
            ),
        )
        db.execute(
            "INSERT INTO generation_events(run_id,data) VALUES(?,?)",
            (lease.run_id, event),
        )
        return True
